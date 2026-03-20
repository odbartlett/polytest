"""Polymarket Whale Bot — entry point.

Wires all services together and runs the async event loop.

SIMULATION MODE (default, SIMULATION_MODE=True):
  Real data feeds — no live orders ever placed.
  Signal pipeline:
    TradeEvent (WebSocket)
      → PositionLedger.update()
      → SignalEngine.evaluate()
      → log SignalEvent to DB
      → PaperTrader.execute()   (records virtual position)
      → TelegramAlerter.sim_position_opened()

LIVE MODE (SIMULATION_MODE=False):
  Same pipeline but uses OrderExecutor.execute() to submit real CLOB orders.
  Requires full POLYMARKET_* credentials.

Lifecycle (both modes):
  1. Load and validate Settings
  2. Init Postgres (create tables if missing)
  3. Init Redis
  4. Build service graph (sim vs live)
  5. Start APScheduler
  6. Run initial whitelist refresh
  7. Start WebSocket stream
  8. Handle SIGINT/SIGTERM for graceful shutdown
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alerts.telegram_bot import TelegramAlerter
from config.settings import get_settings
from data.clob_client import CLOBClient, TradeEvent
from data.gamma_client import GammaClient
from data.websocket_stream import stream_market_trades, stream_trades
from db.session import close_db, init_db
from execution.position_tracker import PositionTracker
from execution.risk_gate import RiskGate
from scheduler.jobs import register_jobs
from scoring.whale_scorer import WhaleScorerService
from scoring.whitelist_manager import WhitelistManager
from signals.position_ledger import PositionLedger
from signals.signal_engine import SignalEngine, SignalDecision

# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(sys.stdout),
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Application class
# ---------------------------------------------------------------------------


class WhaleBot:
    """Top-level application orchestrator."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._shutdown_event = asyncio.Event()
        self._ws_task: Optional[asyncio.Task[None]] = None

        # Services (populated in _build_services)
        self._redis: Optional[aioredis.Redis] = None  # type: ignore[type-arg]
        self._clob_client: Optional[CLOBClient] = None
        self._alerter: Optional[TelegramAlerter] = None
        self._whitelist_manager: Optional[WhitelistManager] = None
        self._ledger: Optional[PositionLedger] = None
        self._signal_engine: Optional[SignalEngine] = None
        self._risk_gate: Optional[RiskGate] = None
        self._position_tracker: Optional[PositionTracker] = None
        self._scheduler: Optional[AsyncIOScheduler] = None

        # Mode-specific
        self._paper_trader = None
        self._performance_tracker = None
        self._market_monitor = None
        self._order_executor = None  # live mode only
        self._monitor_task: Optional[asyncio.Task[None]] = None

    async def run(self) -> None:
        """Main async entry point."""
        mode = "SIMULATION" if self._settings.SIMULATION_MODE else "LIVE"
        logger.info("bot.starting", mode=mode, settings=self._settings.masked_dict())

        try:
            # Step 1: Init database
            await init_db()

            # Step 2: Build service graph
            await self._build_services()

            # Step 3: Start scheduler
            self._scheduler.start()  # type: ignore[union-attr]
            logger.info("scheduler.started")

            # Step 4: Initial whitelist refresh
            logger.info("bot.initial_whitelist_refresh.starting")
            await self._whitelist_manager.refresh_whitelist()  # type: ignore[union-attr]

            whitelist = await self._whitelist_manager.get_whitelist()  # type: ignore[union-attr]
            wallet_addresses = [s.wallet_address for s in whitelist]
            logger.info("bot.whitelist_ready", wallet_count=len(wallet_addresses))

            # Step 5: Send startup notice
            if self._settings.SIMULATION_MODE:
                assert self._paper_trader is not None
                virtual_bankroll = await self._paper_trader.get_sim_bankroll()
                await self._alerter.sim_startup_notice(  # type: ignore[union-attr]
                    virtual_bankroll=virtual_bankroll,
                    whitelist_count=len(wallet_addresses),
                )
            else:
                bankroll = await self._risk_gate.get_bankroll()  # type: ignore[union-attr]
                await self._alerter.startup_notice(  # type: ignore[union-attr]
                    bankroll=bankroll,
                    whitelist_count=len(wallet_addresses),
                )

            # Step 6: Start WebSocket stream
            if self._settings.SIMULATION_MODE:
                # Use public market channel — no wallet whitelist needed.
                # Discover alpha markets (politics, elections, finance) and
                # stream every large trade in those markets.
                gamma = GammaClient()
                alpha_markets = await gamma.get_alpha_markets(
                    min_volume=10_000.0,
                    limit=100,
                )
                token_ids = [
                    tid
                    for m in alpha_markets
                    for tid in m.token_ids
                ]
                logger.info(
                    "bot.alpha_markets_ready",
                    markets=len(alpha_markets),
                    tokens=len(token_ids),
                    sample=alpha_markets[0].question[:60] if alpha_markets else "none",
                )
                self._ws_task = asyncio.create_task(
                    stream_market_trades(
                        token_ids=token_ids,
                        on_trade=self._handle_trade,
                        shutdown_event=self._shutdown_event,
                        min_size_usdc=self._settings.MIN_WHALE_TRADE_SIZE,
                    )
                )
                logger.info("bot.running", mode=mode, alpha_markets=len(alpha_markets))
            else:
                # Live mode: subscribe to whitelisted whale wallet user channels
                self._ws_task = asyncio.create_task(
                    stream_trades(
                        wallet_addresses=wallet_addresses,
                        on_trade=self._handle_trade,
                        shutdown_event=self._shutdown_event,
                    )
                )
                logger.info("bot.running", mode=mode, wallet_count=len(wallet_addresses))

            # Start monitoring dashboard (FastAPI on PORT env var, default 8080)
            monitor_port = int(os.getenv("PORT", "8080"))
            self._monitor_task = asyncio.create_task(self._serve_monitor(monitor_port))
            logger.info("monitor.started", port=monitor_port)

            # Block until shutdown signal
            await self._shutdown_event.wait()

        except Exception as exc:
            logger.critical("bot.fatal_error", error=str(exc), exc_info=True)
            if self._alerter:
                try:
                    await self._alerter.error_alert("main", str(exc))
                except Exception:
                    pass
            raise
        finally:
            await self._shutdown()

    async def _handle_trade(self, trade: TradeEvent) -> None:
        """Non-blocking trade event handler — wraps the full signal pipeline."""
        asyncio.create_task(self._process_trade_pipeline(trade))

    async def _process_trade_pipeline(self, trade: TradeEvent) -> None:
        """Full signal pipeline for a single trade event."""
        try:
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(
                wallet=trade.wallet_address,
                market=trade.market_id,
                side=trade.side,
                size_usdc=trade.size_usdc,
                mode="SIM" if self._settings.SIMULATION_MODE else "LIVE",
            )

            logger.debug("pipeline.trade_received")

            # 1. Update position ledger
            assert self._ledger is not None
            classification = await self._ledger.update(
                wallet=trade.wallet_address,
                market_id=trade.market_id,
                token_id=trade.token_id,
                side=trade.side,
                size=trade.size_usdc,
                price=trade.price,
            )
            logger.debug("pipeline.ledger_updated", classification=classification.value)

            # Copy-exit: when aggregate market position flips direction, close our copy.
            # A large sell reversing a previous buy is a bearish signal from the market.
            from signals.position_ledger import TradeClassification as TC
            if classification == TC.FLIP and trade.side == "SELL":
                asyncio.create_task(self._try_copy_exit(trade.market_id, trade.size_usdc))

            # 2. Evaluate signal (includes SELL→BUY-NO conversion and latency measurement)
            assert self._signal_engine is not None
            signal = await self._signal_engine.evaluate(trade)

            # Cache signal latency for monitoring (use signal's latency_ms when available)
            if signal.latency_ms > 0:
                asyncio.create_task(self._cache_latency(signal.latency_ms))

            # 3. Gate rejection — log and exit early
            if not signal.should_trade:
                asyncio.create_task(_persist_signal_event(trade, signal, exec_gate_failed=None))
                logger.info("pipeline.signal_rejected", reason=signal.reason, gate=signal.gate_failed)
                if signal.gate_failed != "TRADE_IS_BUY" and self._alerter:
                    try:
                        await self._alerter.trade_skipped(
                            market=trade.market_id,
                            reason=signal.reason,
                            whale_wallet=trade.wallet_address,
                        )
                    except Exception:
                        pass
                return

            # 4. Fetch market metadata (use shared cache to avoid duplicate CLOB calls)
            from signals.signal_engine import _market_cache
            assert self._clob_client is not None
            market = _market_cache.get(trade.market_id)
            if market is None:
                market = await self._clob_client.get_market(trade.market_id)
                _market_cache[trade.market_id] = market

            # 5. Alert: whale entry detected
            assert self._alerter is not None
            await self._alerter.whale_entry_detected(
                wallet=trade.wallet_address,
                market=market.question,
                side=trade.side,
                size=trade.size_usdc,
                price=trade.price,
                score=signal.whale_score,
            )

            # 6. Execute (sim or live)
            if self._settings.SIMULATION_MODE:
                assert self._paper_trader is not None
                exec_result = await self._paper_trader.execute(
                    signal=signal,
                    market=market,
                    copied_from_wallet=trade.wallet_address,
                )
            else:
                assert self._order_executor is not None
                exec_result = await self._order_executor.execute(
                    signal=signal,
                    market=market,
                    copied_from_wallet=trade.wallet_address,
                )

            # 7. Log signal event now that we know the actual execution outcome.
            # gate_failed = None if position opened, executor's gate label if rejected.
            asyncio.create_task(
                _persist_signal_event(
                    trade, signal,
                    exec_gate_failed=None if exec_result.success else exec_result.gate_failed,
                )
            )

            # 8. Persist the underlying trade record
            asyncio.create_task(
                _persist_trade_record(trade, signal_generated=exec_result.success, skip_reason=None)
            )

            logger.info(
                "pipeline.complete",
                success=exec_result.success,
                order_id=exec_result.order_id,
                position_id=exec_result.position_id,
                mode="SIM" if self._settings.SIMULATION_MODE else "LIVE",
            )

        except Exception as exc:
            logger.error("pipeline.unhandled_error", error=str(exc), exc_info=True)
            if self._alerter:
                try:
                    await self._alerter.error_alert("pipeline", f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Service construction
    # ------------------------------------------------------------------

    async def _build_services(self) -> None:
        settings = self._settings

        # Redis
        self._redis = await _create_redis(settings.REDIS_URL)

        # CLOB client (kept open for the process lifetime)
        self._clob_client = CLOBClient()
        await self._clob_client.__aenter__()

        # Alerter (gracefully no-ops if Telegram not configured)
        self._alerter = TelegramAlerter()

        # Scorer + whitelist manager
        scorer = WhaleScorerService(redis_client=self._redis)
        self._whitelist_manager = WhitelistManager(
            redis_client=self._redis, scorer=scorer
        )

        # Position ledger
        self._ledger = PositionLedger(redis_client=self._redis)

        # Signal engine
        self._signal_engine = SignalEngine(
            clob_client=self._clob_client,
            position_ledger=self._ledger,
            whitelist_manager=self._whitelist_manager,
            risk_gate=None if settings.SIMULATION_MODE else self._risk_gate,  # wired below
            redis_client=self._redis,
        )

        if settings.SIMULATION_MODE:
            await self._build_sim_services()
        else:
            await self._build_live_services()

        logger.info("bot.services_initialized", mode="SIM" if settings.SIMULATION_MODE else "LIVE")

    async def _build_sim_services(self) -> None:
        """Build simulation-specific services and scheduler."""
        from simulation.paper_trader import PaperTrader
        from simulation.performance_tracker import PerformanceTracker
        from simulation.market_monitor import MarketMonitor

        settings = self._settings

        # Sim services
        self._paper_trader = PaperTrader(
            clob_client=self._clob_client,
            alerter=self._alerter,
            redis_client=self._redis,
        )
        await self._paper_trader.initialize_bankroll()

        self._performance_tracker = PerformanceTracker(
            redis_client=self._redis,
        )

        self._market_monitor = MarketMonitor(
            clob_client=self._clob_client,
            alerter=self._alerter,
            paper_trader=self._paper_trader,
            redis_client=self._redis,
        )

        # In sim mode we still need a RiskGate for the signal engine's circuit breaker
        # but initialize it without Telegram (already wired to alerter)
        self._risk_gate = RiskGate(redis_client=self._redis, alerter=self._alerter)
        await self._risk_gate.initialize()

        # Re-wire signal engine with the risk gate now that it's created
        self._signal_engine._risk_gate = self._risk_gate  # type: ignore[union-attr]

        # Scheduler with sim jobs
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        register_jobs(
            scheduler=self._scheduler,
            whitelist_manager=self._whitelist_manager,
            alerter=self._alerter,
            paper_trader=self._paper_trader,
            performance_tracker=self._performance_tracker,
            market_monitor=self._market_monitor,
            sim_mark_interval_minutes=settings.SIM_MARK_INTERVAL_MINUTES,
            sim_report_interval_hours=settings.SIM_REPORT_INTERVAL_HOURS,
        )

    async def _build_live_services(self) -> None:
        """Build live-trading services and scheduler."""
        from execution.order_executor import OrderExecutor

        # Risk gate
        self._risk_gate = RiskGate(redis_client=self._redis, alerter=self._alerter)
        await self._risk_gate.initialize()

        # Re-wire signal engine with risk gate
        self._signal_engine._risk_gate = self._risk_gate  # type: ignore[union-attr]

        # Order execution + position tracking
        self._order_executor = OrderExecutor(
            clob_client=self._clob_client,
            alerter=self._alerter,
        )
        self._position_tracker = PositionTracker(
            clob_client=self._clob_client,
            redis_client=self._redis,
        )

        # Scheduler with live jobs
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        register_jobs(
            scheduler=self._scheduler,
            whitelist_manager=self._whitelist_manager,
            alerter=self._alerter,
            order_executor=self._order_executor,
            position_tracker=self._position_tracker,
            risk_gate=self._risk_gate,
        )

    async def _serve_monitor(self, port: int) -> None:
        """Run the FastAPI monitoring dashboard in the background."""
        import uvicorn
        from monitor.api import app as monitor_app

        config = uvicorn.Config(monitor_app, host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _shutdown(self) -> None:
        """Graceful shutdown: cancel tasks, close connections, flush logs."""
        logger.info("bot.shutdown.starting")

        self._shutdown_event.set()

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await asyncio.wait_for(self._monitor_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await asyncio.wait_for(self._ws_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

        if self._alerter:
            try:
                await self._alerter.shutdown_notice()
            except Exception:
                pass

        if self._clob_client:
            try:
                await self._clob_client.__aexit__(None, None, None)
            except Exception:
                pass

        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass

        try:
            await close_db()
        except Exception:
            pass

        logger.info("bot.shutdown.complete")

    async def _try_copy_exit(self, market_id: str, trigger_size_usdc: float) -> None:
        """Close our open position when the aggregate market direction flips.

        A FLIP classification means a large SELL has reversed the accumulated
        position tracked by the ledger — a bearish signal.  We close any open
        simulated position in that market at the current mid-price.
        """
        if not self._settings.SIMULATION_MODE or self._market_monitor is None:
            return
        from db.models import BotPosition, PositionStatus
        from db.session import AsyncSessionLocal
        from sqlalchemy import select

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotPosition).where(
                        BotPosition.market_id == market_id,
                        BotPosition.status == PositionStatus.OPEN,
                        BotPosition.is_simulated.is_(True),
                    ).limit(1)
                )
                pos = result.scalar_one_or_none()

            if pos is None:
                return

            current_price = await self._market_monitor._get_mid_price(pos.token_id)  # type: ignore[attr-defined]
            if current_price is None:
                current_price = pos.current_price or pos.entry_price

            logger.info(
                "pipeline.copy_exit",
                market=market_id,
                position_id=pos.id,
                exit_price=round(current_price, 4),
                trigger_sell_size=round(trigger_size_usdc, 2),
            )
            await self._market_monitor._force_close_position(pos, current_price, "COPY_EXIT")  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("pipeline.copy_exit_failed", market=market_id, error=str(exc))

    async def _cache_latency(self, latency_ms: float) -> None:
        """Push latency sample into Redis ring buffer for monitoring API."""
        if self._redis is None:
            return
        try:
            key = "bot:latency:samples"
            await self._redis.lpush(key, str(round(latency_ms, 1)))
            await self._redis.ltrim(key, 0, 999)   # keep last 1000 samples
            await self._redis.expire(key, 86400)    # 24h TTL
        except Exception:
            pass

    def handle_signal(self, sig: signal.Signals) -> None:
        """Signal handler for SIGINT/SIGTERM."""
        logger.info("bot.signal_received", signal=sig.name)
        self._shutdown_event.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_redis(url: str) -> aioredis.Redis:  # type: ignore[type-arg]
    """Create and test a Redis connection."""
    try:
        client = aioredis.from_url(url, decode_responses=False, socket_timeout=5)
        await client.ping()
        logger.info("redis.connected", url=url.split("@")[-1])
        return client
    except Exception as exc:
        logger.error("redis.connection_failed", error=str(exc))
        raise


async def _persist_signal_event(
    trade: TradeEvent,
    signal: SignalDecision,
    exec_gate_failed: "str | None" = None,
) -> None:
    """Log every signal evaluation to signal_events for funnel analytics.

    For signals that passed all gates (signal.should_trade=True), we call this
    AFTER execution so we can record the actual outcome:
      - exec_gate_failed=None  → position was opened → signal_result='EXECUTED'
      - exec_gate_failed=str   → executor rejected it (e.g. PRICE_ASSERTION_FAILED,
                                  DUPLICATE_POSITION) → signal_result='SKIPPED'
                                  with that label as gate_failed
    """
    from db.session import AsyncSessionLocal

    # Determine the true gate and result after considering executor outcome
    if not signal.should_trade:
        # Failed a signal gate — use signal's own gate label
        result = "SKIPPED"
        gate = signal.gate_failed
        reason = signal.reason
    elif exec_gate_failed is not None:
        # Passed all signal gates but executor rejected — show executor's gate
        result = "SKIPPED"
        gate = exec_gate_failed
        reason = f"Executor rejected: {exec_gate_failed}"
    else:
        # Passed everything, position opened
        result = "EXECUTED"
        gate = None
        reason = signal.reason

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                from sqlalchemy import text
                await session.execute(
                    text(
                        """
                        INSERT INTO signal_events (
                            wallet_address, market_id, whale_score, score_tier,
                            trade_size_usdc, signal_result, gate_failed,
                            skip_reason, copy_size_usdc, evaluated_at
                        ) VALUES (
                            :wallet, :market, :score, :tier,
                            :size, :result, :gate,
                            :reason, :copy_size, :ts
                        )
                        """
                    ),
                    {
                        "wallet": trade.wallet_address,
                        "market": trade.market_id,
                        "score": signal.whale_score,
                        "tier": _score_tier(signal.whale_score),
                        "size": trade.size_usdc,
                        "result": result,
                        "gate": gate,
                        "reason": reason,
                        "copy_size": signal.copy_size_usdc if result == "EXECUTED" else None,
                        "ts": datetime.now(tz=timezone.utc),
                    },
                )
    except Exception as exc:
        logger.error("persist_signal_event.failed", error=str(exc))


async def _persist_trade_record(
    trade: TradeEvent,
    signal_generated: bool,
    skip_reason: "str | None",
) -> None:
    """Persist a Trade record to Postgres (background task)."""
    from db.models import Trade, SideEnum
    from db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                record = Trade(
                    wallet_address=trade.wallet_address,
                    market_id=trade.market_id,
                    token_id=trade.token_id,
                    side=SideEnum.BUY if trade.side == "BUY" else SideEnum.SELL,
                    price=trade.price,
                    size_usdc=trade.size_usdc,
                    timestamp=trade.timestamp,
                    signal_generated=signal_generated,
                    signal_reason_skipped=skip_reason,
                )
                session.add(record)
    except Exception as exc:
        logger.error("persist_trade.failed", error=str(exc))


def _score_tier(score: float) -> str:
    if score is None:
        return "unknown"
    if 55 <= score < 65:
        return "55-65"
    elif 65 <= score < 75:
        return "65-75"
    elif 75 <= score < 85:
        return "75-85"
    elif score >= 85:
        return "85+"
    return "unknown"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _main() -> None:
    bot = WhaleBot()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.handle_signal, sig)

    await bot.run()


def run() -> None:
    """Script entry point (referenced in pyproject.toml)."""
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
