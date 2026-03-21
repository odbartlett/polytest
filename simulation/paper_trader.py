"""Paper-trading order executor.

Simulates order execution using live Polymarket orderbook data without
ever submitting anything to the CLOB. Every simulated fill is persisted
to the bot_positions table with is_simulated=True so the full analytics
pipeline can run on real market data.

Fill price simulation:
  1. Walk the real ask levels to compute a volume-weighted average price
     for the copy_size (realistic market impact).
  2. Add SIM_FILL_SLIPPAGE on top (models latency / spread crossing).
  3. Record entry price, shares, and opening metadata in Postgres.
  4. Log a signal_event audit row for funnel analysis.

Bankroll accounting:
  - sim:bankroll  = liquid (uninvested) cash.  Starts at SIM_BANKROLL_USDC.
  - On position open  : bankroll -= size_usdc
  - On position close : bankroll += size_usdc + realized_pnl
                        (i.e. bankroll += exit_price * shares_held)
  - Deployed capital  = SUM(open positions.size_usdc)  [queried from DB]
  - Portfolio value   = bankroll + SUM(current_price * shares_held)
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import get_settings
from data.clob_client import CLOBClient, Market, Orderbook
from db.models import BotOrder, BotPosition, OrderStatus, PositionStatus, SideEnum
from db.session import AsyncSessionLocal
from execution.order_executor import ExecutionResult
from signals.signal_engine import SignalDecision

logger = structlog.get_logger(__name__)

_settings = get_settings()

# Redis keys for sim bankroll
SIM_BANKROLL_KEY = "sim:bankroll"
SIM_PEAK_BANKROLL_KEY = "sim:peak_bankroll"


def _score_tier(score: float) -> str:
    if 55 <= score < 65:
        return "55-65"
    elif 65 <= score < 75:
        return "65-75"
    elif 75 <= score < 85:
        return "75-85"
    elif score >= 85:
        return "85+"
    return "unknown"


def _compute_vwap_fill(orderbook: Orderbook, size_usdc: float) -> float:
    """Walk ask levels and return VWAP fill price for the given USDC size.

    Returns the best ask if the full size fits in the first level, otherwise
    computes a weighted average across as many levels as needed.
    Adds SIM_FILL_SLIPPAGE on top.
    """
    settings = get_settings()
    remaining = size_usdc
    cost = 0.0
    total_shares = 0.0

    for level in orderbook.asks:
        if level.price <= 0:
            continue
        level_capacity_usdc = level.price * level.size
        fill_usdc = min(remaining, level_capacity_usdc)
        shares_at_level = fill_usdc / level.price
        cost += fill_usdc
        total_shares += shares_at_level
        remaining -= fill_usdc
        if remaining <= 0:
            break

    if total_shares <= 0:
        best_ask = orderbook.asks[0].price if orderbook.asks else 0.5
        vwap = best_ask
    else:
        vwap = cost / total_shares

    return vwap * (1 + settings.SIM_FILL_SLIPPAGE)


class PaperTrader:
    """Simulates order execution for the paper-trading mode."""

    def __init__(
        self,
        clob_client: CLOBClient,
        alerter: object,
        redis_client: object,
    ) -> None:
        self._clob = clob_client
        self._alerter = alerter
        self._redis = redis_client
        self._settings = get_settings()
        # Per-market locks prevent duplicate positions from concurrent trade events
        self._market_locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        signal: SignalDecision,
        market: Market,
        copied_from_wallet: str,
        strategy: str = "COPY",
    ) -> ExecutionResult:
        """Simulate a buy and record the paper position.

        Returns early (no position opened) if:
          - No token data on the market
          - An open position for the same market+strategy already exists
          - Insufficient available cash
        """
        if not market.tokens:
            return ExecutionResult(success=False, reason="No tokens for market", gate_failed="NO_MARKET_TOKENS")

        # Serialize concurrent fills for the same market to prevent race-condition
        # duplicates (two trades arriving within milliseconds both see no open position).
        lock_key = f"{market.market_id}:{strategy}"
        if lock_key not in self._market_locks:
            self._market_locks[lock_key] = asyncio.Lock()

        async with self._market_locks[lock_key]:
            return await self._execute_locked(signal, market, copied_from_wallet, strategy)

    async def _execute_locked(
        self,
        signal: SignalDecision,
        market: Market,
        copied_from_wallet: str,
        strategy: str = "COPY",
    ) -> ExecutionResult:
        """Inner execute — called only while holding the per-market lock."""
        # Use the token the whale actually traded (from signal), not just tokens[0].
        # tokens[0] may be the opposite outcome (e.g. NO when whale bought YES),
        # causing fills at ~$1.00 on near-resolved tokens.
        token_id = signal.token_id if signal.token_id else market.tokens[0].token_id
        token = next((t for t in market.tokens if t.token_id == token_id), market.tokens[0])

        # --- Guard 1: strategy-aware duplicate check ---
        # COPY: block any open position in the market (prevents YES+NO simultaneously).
        # MICRO/NO_FLIP: only block same-strategy positions in the same market.
        existing = await self._get_open_position(market.market_id, strategy)
        if existing is not None:
            logger.info(
                "paper_trader.duplicate_skipped",
                market=market.market_id,
                existing_position_id=existing.id,
            )
            return ExecutionResult(
                success=False,
                reason=f"Already have open position {existing.id} in this market",
                gate_failed="DUPLICATE_POSITION",
            )

        # --- Guard 2: enough liquid cash ---
        available = await self.get_available_cash()
        if available < signal.copy_size_usdc:
            logger.info(
                "paper_trader.insufficient_cash",
                available=round(available, 2),
                needed=signal.copy_size_usdc,
            )
            return ExecutionResult(
                success=False,
                reason=f"Insufficient cash: ${available:.2f} available, ${signal.copy_size_usdc:.2f} needed",
                gate_failed="INSUFFICIENT_CASH",
            )

        # --- Compute simulated fill price from live orderbook ---
        try:
            orderbook = await self._clob.get_orderbook(token_id)
            fill_price = _compute_vwap_fill(orderbook, signal.copy_size_usdc)
        except Exception as exc:
            logger.warning("paper_trader.orderbook_fetch_failed", error=str(exc))
            fill_price = 0.5 * (1 + self._settings.SIM_FILL_SLIPPAGE)

        if fill_price <= 0:
            fill_price = 0.5

        # Pre-execution price assertion: abort if fill price is outside the valid range.
        # Uses SIM_FILL_PRICE_MAX (wider than MAX_ENTRY_PRICE) to allow for market
        # movement between the whale's trade and our fill — catches token-side inversions
        # ($0.99 fills) while accepting legitimate post-whale-trade price shifts.
        fill_price_max = self._settings.SIM_FILL_PRICE_MAX
        if not (self._settings.MIN_ENTRY_PRICE <= fill_price <= fill_price_max):
            direction = "TOO_HIGH" if fill_price > fill_price_max else "TOO_LOW"
            logger.warning(
                "paper_trader.price_assertion_failed",
                token_id=token_id,
                fill_price=round(fill_price, 4),
                whale_trade_price=round(signal.copy_size_usdc, 4),
                direction=direction,
                min_price=self._settings.MIN_ENTRY_PRICE,
                max_price=fill_price_max,
            )
            return ExecutionResult(
                success=False,
                reason=f"Price assertion failed: fill price {fill_price:.4f} outside [{self._settings.MIN_ENTRY_PRICE}, {fill_price_max}] ({direction})",
                gate_failed="PRICE_ASSERTION_FAILED",
            )

        shares = signal.copy_size_usdc / fill_price
        now = datetime.now(tz=timezone.utc)
        tier = _score_tier(signal.whale_score)

        # --- Persist paper position (is_simulated=True) ---
        async with AsyncSessionLocal() as session:
            async with session.begin():
                position = BotPosition(
                    market_id=market.market_id,
                    market_question=market.question,
                    market_category=market.category,
                    token_id=token_id,
                    side=SideEnum.BUY,
                    entry_price=fill_price,
                    size_usdc=signal.copy_size_usdc,
                    shares_held=shares,
                    copied_from_wallet=copied_from_wallet,
                    whale_score_at_entry=signal.whale_score,
                    score_tier=tier,
                    status=PositionStatus.OPEN,
                    is_simulated=True,
                    strategy=strategy,
                    current_price=fill_price,
                    unrealized_pnl_usdc=0.0,
                    last_marked_at=now,
                    opened_at=now,
                    signal_roi_score=signal.roi_score,
                    signal_consistency_score=signal.consistency_score,
                )
                session.add(position)
                await session.flush()
                position_id = position.id

                # Synthetic "filled" order record for auditing
                bot_order = BotOrder(
                    bot_position_id=position_id,
                    clob_order_id=f"SIM-{position_id}-{int(now.timestamp())}",
                    market_id=market.market_id,
                    token_id=token_id,
                    side=SideEnum.BUY,
                    limit_price=fill_price,
                    size_usdc=signal.copy_size_usdc,
                    status=OrderStatus.FILLED,
                    strategy=strategy,
                    placed_at=now,
                    filled_at=now,
                    fill_price=fill_price,
                )
                session.add(bot_order)

        # --- Cache resolution time for time-based exit checks ---
        if market.resolution_time is not None:
            try:
                await self._redis.setex(  # type: ignore[union-attr]
                    f"pos:{position_id}:resolution_time",
                    86400 * 30,  # 30-day TTL
                    market.resolution_time.isoformat(),
                )
            except Exception:
                pass

        # --- Deduct from liquid bankroll ---
        await self._deduct_from_bankroll(signal.copy_size_usdc)

        logger.info(
            "paper_trader.position_opened",
            position_id=position_id,
            market=market.market_id,
            question=market.question,
            fill_price=fill_price,
            shares=round(shares, 4),
            size_usdc=signal.copy_size_usdc,
            available_cash=round(available - signal.copy_size_usdc, 2),
            whale_score=signal.whale_score,
            tier=tier,
            copied_from=copied_from_wallet,
        )

        # --- Telegram alert ---
        try:
            await self._alerter.sim_position_opened(  # type: ignore[attr-defined]
                market=market.question,
                size=signal.copy_size_usdc,
                fill_price=fill_price,
                shares=shares,
                whale_score=signal.whale_score,
                copy_of=copied_from_wallet,
            )
        except Exception as exc:
            logger.warning("paper_trader.alert_failed", error=str(exc))

        return ExecutionResult(
            success=True,
            order_id=f"SIM-{position_id}",
            position_id=position_id,
            fill_price=fill_price,
        )

    # ------------------------------------------------------------------
    # Bankroll helpers
    # ------------------------------------------------------------------

    async def get_available_cash(self) -> float:
        """Return liquid (uninvested) cash remaining."""
        try:
            val = await self._redis.get(SIM_BANKROLL_KEY)  # type: ignore[union-attr]
            return float(val) if val else self._settings.SIM_BANKROLL_USDC
        except Exception:
            return self._settings.SIM_BANKROLL_USDC

    # Keep the old name as an alias so existing callers don't break
    async def get_sim_bankroll(self) -> float:
        return await self.get_available_cash()

    async def get_deployed_capital(self) -> float:
        """Return total USDC currently locked in open paper positions."""
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(func.sum(BotPosition.size_usdc)).where(
                        BotPosition.status == PositionStatus.OPEN,
                        BotPosition.is_simulated.is_(True),
                    )
                )
                total = result.scalar_one_or_none()
                return float(total) if total else 0.0
        except Exception:
            return 0.0

    async def get_portfolio_value(self) -> dict[str, float]:
        """Return a snapshot of the current portfolio state.

        Returns:
            dict with keys: available_cash, deployed_capital,
                            unrealized_pnl, portfolio_value
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(
                        func.sum(BotPosition.size_usdc),
                        func.sum(BotPosition.unrealized_pnl_usdc),
                        func.count(BotPosition.id),
                    ).where(
                        BotPosition.status == PositionStatus.OPEN,
                        BotPosition.is_simulated.is_(True),
                    )
                )
                row = result.one()
                deployed = float(row[0] or 0)
                unrealized = float(row[1] or 0)
                count = int(row[2] or 0)
        except Exception:
            deployed = unrealized = 0.0
            count = 0

        available = await self.get_available_cash()
        portfolio_value = available + deployed + unrealized

        return {
            "available_cash": round(available, 2),
            "deployed_capital": round(deployed, 2),
            "unrealized_pnl": round(unrealized, 2),
            "portfolio_value": round(portfolio_value, 2),
            "open_positions": count,
        }

    async def restore_bankroll(self, amount: float) -> None:
        """Add amount back to liquid cash (called when a position closes).

        amount = exit_price * shares_held  (the actual proceeds received)
        """
        try:
            current = await self.get_available_cash()
            new_balance = current + amount
            await self._redis.set(SIM_BANKROLL_KEY, str(new_balance))  # type: ignore[union-attr]
            # Update peak if we hit a new high
            try:
                peak_raw = await self._redis.get(SIM_PEAK_BANKROLL_KEY)  # type: ignore[union-attr]
                peak = float(peak_raw) if peak_raw else new_balance
                if new_balance > peak:
                    await self._redis.set(SIM_PEAK_BANKROLL_KEY, str(new_balance))  # type: ignore[union-attr]
            except Exception:
                pass
            logger.debug("paper_trader.bankroll_restored", added=round(amount, 2), balance=round(new_balance, 2))
        except Exception as exc:
            logger.warning("paper_trader.bankroll_restore_failed", error=str(exc))

    async def initialize_bankroll(self) -> None:
        """Set starting bankroll if not already set in Redis."""
        try:
            existing = await self._redis.get(SIM_BANKROLL_KEY)  # type: ignore[union-attr]
            if existing is None:
                initial = self._settings.SIM_BANKROLL_USDC
                await self._redis.set(SIM_BANKROLL_KEY, str(initial))  # type: ignore[union-attr]
                await self._redis.set(SIM_PEAK_BANKROLL_KEY, str(initial))  # type: ignore[union-attr]
                logger.info("paper_trader.bankroll_initialized", bankroll=initial)
        except Exception as exc:
            logger.warning("paper_trader.bankroll_init_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _deduct_from_bankroll(self, amount: float) -> None:
        """Subtract deployed capital from liquid cash."""
        try:
            current = await self.get_available_cash()
            new_balance = max(0.0, current - amount)
            await self._redis.set(SIM_BANKROLL_KEY, str(new_balance))  # type: ignore[union-attr]
            logger.debug("paper_trader.bankroll_deducted", deducted=round(amount, 2), balance=round(new_balance, 2))
        except Exception as exc:
            logger.warning("paper_trader.bankroll_deduct_failed", error=str(exc))

    async def _get_open_position(
        self, market_id: str, strategy: str = "COPY"
    ) -> Optional[BotPosition]:
        """Return an existing open position for this market, or None.

        COPY strategy: blocks any open position in the market (prevents YES+NO).
        MICRO/NO_FLIP: only blocks same-strategy positions in the same market.
        """
        try:
            async with AsyncSessionLocal() as session:
                filters = [
                    BotPosition.market_id == market_id,
                    BotPosition.status == PositionStatus.OPEN,
                    BotPosition.is_simulated.is_(True),
                ]
                if strategy != "COPY":
                    filters.append(BotPosition.strategy == strategy)
                result = await session.execute(
                    select(BotPosition).where(*filters).limit(1)
                )
                return result.scalar_one_or_none()
        except Exception as exc:
            logger.warning("paper_trader.position_lookup_failed", error=str(exc))
            return None
