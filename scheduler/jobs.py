"""APScheduler job definitions.

All jobs are registered on the provided AsyncIOScheduler instance.
Each job wraps the underlying async service call in robust error handling
so that a single job failure never crashes the scheduler.

In SIMULATION_MODE, live-trading jobs (order monitoring, bankroll sync,
stale cleanup) are replaced by simulation jobs (mark-to-market, resolution
checks, performance reporting).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from execution.order_executor import OrderExecutor
    from execution.position_tracker import PositionTracker
    from execution.risk_gate import RiskGate
    from scoring.whitelist_manager import WhitelistManager
    from alerts.telegram_bot import TelegramAlerter
    from simulation.paper_trader import PaperTrader
    from simulation.performance_tracker import PerformanceTracker
    from simulation.market_monitor import MarketMonitor

from db.models import DailyPnL
from db.session import AsyncSessionLocal
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = structlog.get_logger(__name__)


def register_jobs(
    scheduler: AsyncIOScheduler,
    whitelist_manager: "WhitelistManager",
    alerter: "TelegramAlerter",
    # Live-mode services (None in sim mode)
    order_executor: "OrderExecutor | None" = None,
    position_tracker: "PositionTracker | None" = None,
    risk_gate: "RiskGate | None" = None,
    # Sim-mode services (None in live mode)
    paper_trader: "PaperTrader | None" = None,
    performance_tracker: "PerformanceTracker | None" = None,
    market_monitor: "MarketMonitor | None" = None,
    sim_mark_interval_minutes: int = 2,
    sim_report_interval_hours: int = 6,
    fast_check_interval_seconds: int = 60,
) -> None:
    """Register all scheduled jobs on the given scheduler.

    Idempotent — safe to call multiple times (APScheduler deduplicates by ID).
    Automatically chooses live vs simulation job sets based on which services
    are provided.
    """
    simulation_mode = paper_trader is not None

    # ------------------------------------------------------------------
    # Whitelist refresh — every day at 02:00 UTC (both modes)
    # ------------------------------------------------------------------
    scheduler.add_job(
        func=_whitelist_refresh_job,
        trigger=CronTrigger(hour=2, minute=0, timezone="UTC"),
        id="whitelist_refresh",
        name="Nightly whitelist refresh",
        replace_existing=True,
        kwargs={
            "whitelist_manager": whitelist_manager,
            "alerter": alerter,
        },
    )

    if simulation_mode:
        _register_sim_jobs(
            scheduler=scheduler,
            alerter=alerter,
            paper_trader=paper_trader,
            performance_tracker=performance_tracker,
            market_monitor=market_monitor,
            mark_interval_minutes=sim_mark_interval_minutes,
            report_interval_hours=sim_report_interval_hours,
            fast_check_interval_seconds=fast_check_interval_seconds,
        )
    else:
        _register_live_jobs(
            scheduler=scheduler,
            order_executor=order_executor,
            position_tracker=position_tracker,
            risk_gate=risk_gate,
            alerter=alerter,
        )

    mode_str = "simulation" if simulation_mode else "live"
    logger.info("scheduler.jobs_registered", mode=mode_str)


def _register_live_jobs(
    scheduler: AsyncIOScheduler,
    order_executor: "OrderExecutor | None",
    position_tracker: "PositionTracker | None",
    risk_gate: "RiskGate | None",
    alerter: "TelegramAlerter",
) -> None:
    """Register live-trading scheduler jobs."""

    # Daily P&L snapshot — every day at 23:55 UTC
    scheduler.add_job(
        func=_daily_pnl_snapshot_job,
        trigger=CronTrigger(hour=23, minute=55, timezone="UTC"),
        id="daily_pnl_snapshot",
        name="Daily P&L snapshot",
        replace_existing=True,
        kwargs={
            "position_tracker": position_tracker,
            "risk_gate": risk_gate,
            "alerter": alerter,
        },
    )

    # Monitor open orders — every 15 seconds
    scheduler.add_job(
        func=_monitor_orders_job,
        trigger=IntervalTrigger(seconds=15),
        id="monitor_orders",
        name="Monitor open orders",
        replace_existing=True,
        kwargs={"order_executor": order_executor},
    )

    # Bankroll sync — every 60 seconds
    scheduler.add_job(
        func=_bankroll_sync_job,
        trigger=IntervalTrigger(seconds=60),
        id="bankroll_sync",
        name="Bankroll sync",
        replace_existing=True,
        kwargs={
            "position_tracker": position_tracker,
            "risk_gate": risk_gate,
        },
    )

    # Stale order cleanup — every 5 minutes
    scheduler.add_job(
        func=_stale_order_cleanup_job,
        trigger=IntervalTrigger(minutes=5),
        id="stale_order_cleanup",
        name="Stale order cleanup",
        replace_existing=True,
        kwargs={"order_executor": order_executor},
    )


def _register_sim_jobs(
    scheduler: AsyncIOScheduler,
    alerter: "TelegramAlerter",
    paper_trader: "PaperTrader | None",
    performance_tracker: "PerformanceTracker | None",
    market_monitor: "MarketMonitor | None",
    mark_interval_minutes: int,
    report_interval_hours: int,
    fast_check_interval_seconds: int = 60,
) -> None:
    """Register simulation-mode scheduler jobs."""

    # Fast exit check — every 60 seconds (volatile positions and NO_FLIP only)
    scheduler.add_job(
        func=_sim_fast_exit_check_job,
        trigger=IntervalTrigger(seconds=fast_check_interval_seconds),
        id="sim_fast_exit_check",
        name="Sim fast exit check",
        replace_existing=True,
        kwargs={"market_monitor": market_monitor},
    )

    # Mark-to-market — every SIM_MARK_INTERVAL_MINUTES
    scheduler.add_job(
        func=_sim_mark_to_market_job,
        trigger=IntervalTrigger(minutes=mark_interval_minutes),
        id="sim_mark_to_market",
        name="Sim mark-to-market",
        replace_existing=True,
        kwargs={
            "market_monitor": market_monitor,
            "performance_tracker": performance_tracker,
            "alerter": alerter,
        },
    )

    # Resolution check — every 5 minutes
    scheduler.add_job(
        func=_sim_resolution_check_job,
        trigger=IntervalTrigger(minutes=5),
        id="sim_resolution_check",
        name="Sim market resolution check",
        replace_existing=True,
        kwargs={"market_monitor": market_monitor},
    )

    # Performance report — every SIM_REPORT_INTERVAL_HOURS
    scheduler.add_job(
        func=_sim_performance_report_job,
        trigger=IntervalTrigger(hours=report_interval_hours),
        id="sim_performance_report",
        name="Sim performance report",
        replace_existing=True,
        kwargs={
            "performance_tracker": performance_tracker,
            "alerter": alerter,
        },
    )

    # Daily sim snapshot — every day at 23:55 UTC
    scheduler.add_job(
        func=_sim_daily_snapshot_job,
        trigger=CronTrigger(hour=23, minute=55, timezone="UTC"),
        id="sim_daily_snapshot",
        name="Sim daily snapshot",
        replace_existing=True,
        kwargs={
            "performance_tracker": performance_tracker,
            "alerter": alerter,
        },
    )


# ---------------------------------------------------------------------------
# Shared job implementations
# ---------------------------------------------------------------------------


async def _whitelist_refresh_job(
    whitelist_manager: "WhitelistManager",
    alerter: "TelegramAlerter",
) -> None:
    """Refresh the whale whitelist and send a summary alert."""
    logger.info("job.whitelist_refresh.started")
    try:
        result = await whitelist_manager.refresh_whitelist()
        await alerter.whitelist_refreshed(
            added=result.added,
            removed=result.removed,
            retained=result.retained,
        )
        logger.info("job.whitelist_refresh.complete", **result.model_dump())
    except Exception as exc:
        logger.error("job.whitelist_refresh.failed", error=str(exc))
        try:
            await alerter.error_alert("WhitelistManager", str(exc))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Live-mode job implementations
# ---------------------------------------------------------------------------


async def _daily_pnl_snapshot_job(
    position_tracker: "PositionTracker",
    risk_gate: "RiskGate",
    alerter: "TelegramAlerter",
) -> None:
    """Compute daily P&L, persist to DB, and send Telegram summary."""
    logger.info("job.daily_pnl.started")
    try:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        stats = await position_tracker.get_daily_stats(today)
        bankroll = await risk_gate.get_bankroll()
        unrealized_pnl = await position_tracker.compute_unrealized_pnl()

        from config.settings import get_settings
        settings = get_settings()
        starting_bankroll = settings.BANKROLL_USDC

        async with AsyncSessionLocal() as session:
            async with session.begin():
                stmt = pg_insert(DailyPnL).values(
                    date=today,
                    starting_bankroll=starting_bankroll,
                    ending_bankroll=bankroll,
                    realized_pnl=stats["realized_pnl"],
                    unrealized_pnl=unrealized_pnl,
                    trade_count=int(stats["trade_count"]),
                    win_count=int(stats["win_count"]),
                    loss_count=int(stats["loss_count"]),
                ).on_conflict_do_update(
                    index_elements=["date"],
                    set_={
                        "ending_bankroll": bankroll,
                        "realized_pnl": stats["realized_pnl"],
                        "unrealized_pnl": unrealized_pnl,
                        "trade_count": int(stats["trade_count"]),
                        "win_count": int(stats["win_count"]),
                        "loss_count": int(stats["loss_count"]),
                    },
                )
                await session.execute(stmt)

        await alerter.daily_summary(
            pnl=stats["realized_pnl"],
            win_rate=stats["win_rate"],
            trade_count=int(stats["trade_count"]),
            bankroll=bankroll,
        )
        logger.info("job.daily_pnl.complete", date=today, realized_pnl=stats["realized_pnl"])
    except Exception as exc:
        logger.error("job.daily_pnl.failed", error=str(exc))
        try:
            await alerter.error_alert("DailyPnLJob", str(exc))
        except Exception:
            pass


async def _monitor_orders_job(order_executor: "OrderExecutor") -> None:
    """Poll PENDING orders and update fill status."""
    try:
        await order_executor.monitor_open_orders()
    except Exception as exc:
        logger.error("job.monitor_orders.failed", error=str(exc))


async def _bankroll_sync_job(
    position_tracker: "PositionTracker",
    risk_gate: "RiskGate",
) -> None:
    """Recalculate and persist the current bankroll."""
    try:
        new_bankroll = await position_tracker.sync_bankroll()
        await risk_gate.update_bankroll(new_bankroll)
    except Exception as exc:
        logger.error("job.bankroll_sync.failed", error=str(exc))


async def _stale_order_cleanup_job(order_executor: "OrderExecutor") -> None:
    """Cancel any PENDING orders that have exceeded their timeout."""
    try:
        await order_executor.cancel_stale_orders()
    except Exception as exc:
        logger.error("job.stale_order_cleanup.failed", error=str(exc))


# ---------------------------------------------------------------------------
# Simulation-mode job implementations
# ---------------------------------------------------------------------------


async def _sim_fast_exit_check_job(market_monitor: "MarketMonitor") -> None:
    """Quick exit check for volatile and NO_FLIP positions (60-second cadence)."""
    try:
        await market_monitor.fast_exit_check()
    except Exception as exc:
        logger.error("job.sim_fast_exit_check.failed", error=str(exc))


async def _sim_mark_to_market_job(
    market_monitor: "MarketMonitor",
    performance_tracker: "PerformanceTracker",
    alerter: "TelegramAlerter",
) -> None:
    """Update unrealized P&L for all open sim positions."""
    logger.debug("job.sim_mark_to_market.started")
    try:
        marked = await market_monitor.mark_to_market()

        if marked > 0:
            # Fetch summary metrics to send a lightweight update
            metrics = await performance_tracker.compute_metrics()
            await alerter.sim_mark_to_market_update(
                open_positions=metrics.open_positions,
                total_unrealized_pnl=metrics.total_unrealized_pnl_usdc,
                virtual_bankroll=metrics.virtual_bankroll,
            )

        logger.debug("job.sim_mark_to_market.complete", marked=marked)
    except Exception as exc:
        logger.error("job.sim_mark_to_market.failed", error=str(exc))


async def _sim_resolution_check_job(market_monitor: "MarketMonitor") -> None:
    """Auto-close paper positions for resolved markets."""
    try:
        closed = await market_monitor.check_resolutions()
        if closed:
            logger.info("job.sim_resolution_check.closed", count=closed)
    except Exception as exc:
        logger.error("job.sim_resolution_check.failed", error=str(exc))


async def _sim_performance_report_job(
    performance_tracker: "PerformanceTracker",
    alerter: "TelegramAlerter",
) -> None:
    """Generate and send a full simulation performance report."""
    logger.info("job.sim_performance_report.started")
    try:
        metrics = await performance_tracker.compute_metrics()
        report = performance_tracker.generate_report(metrics)
        await alerter.sim_performance_report(report)
        logger.info(
            "job.sim_performance_report.complete",
            total_pnl=metrics.total_realized_pnl_usdc,
            win_rate=metrics.win_rate,
        )
    except Exception as exc:
        logger.error("job.sim_performance_report.failed", error=str(exc))
        try:
            await alerter.error_alert("SimPerformanceReport", str(exc))
        except Exception:
            pass


async def _sim_daily_snapshot_job(
    performance_tracker: "PerformanceTracker",
    alerter: "TelegramAlerter",
) -> None:
    """Persist daily sim metrics to sim_daily_snapshots table."""
    logger.info("job.sim_daily_snapshot.started")
    try:
        metrics = await performance_tracker.compute_metrics()
        await performance_tracker.persist_daily_snapshot(metrics)
        logger.info("job.sim_daily_snapshot.complete")
    except Exception as exc:
        logger.error("job.sim_daily_snapshot.failed", error=str(exc))
        try:
            await alerter.error_alert("SimDailySnapshot", str(exc))
        except Exception:
            pass
