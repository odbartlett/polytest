"""Performance metrics engine for the paper-trading simulation.

Queries Postgres for all simulated positions and computes a comprehensive
set of metrics including P&L, win rate, per-whale attribution, per-category
breakdown, score-tier analysis, and signal funnel statistics.

Metrics are returned as a SimMetrics dataclass and can be formatted into
a Telegram-ready text report via generate_report().
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import func, select, text

from db.models import BotPosition, PositionStatus
from db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)


@dataclass
class TierMetrics:
    tier: str
    total_trades: int
    win_count: int
    loss_count: int
    total_pnl: float
    avg_pnl: float
    win_rate: float


@dataclass
class WalletMetrics:
    wallet: str
    total_trades: int
    win_count: int
    total_pnl: float
    avg_pnl: float
    win_rate: float


@dataclass
class SignalFunnelStats:
    total_evaluated: int
    executed: int
    skipped_gate1: int  # TRADE_IS_BUY
    skipped_gate2: int  # TRADE_SIZE_MIN
    skipped_gate3: int  # WHALE_SCORE_MIN
    skipped_gate4: int  # MARKET_OI_MIN
    skipped_gate5: int  # ORDERBOOK_DEPTH
    skipped_gate6: int  # POSITION_CAP
    skipped_gate7: int  # TIME_TO_RESOLUTION
    skipped_gate8: int  # CIRCUIT_BREAKER
    risk_rejected: int
    execution_rate: float  # executed / total_evaluated


@dataclass
class SimMetrics:
    generated_at: datetime

    # Overall P&L
    starting_bankroll: float
    virtual_bankroll: float
    total_realized_pnl: float
    total_unrealized_pnl: float
    total_pnl: float
    total_return_pct: float

    # Trade stats
    open_positions: int
    total_closed: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_pnl_per_trade: float
    best_trade_pnl: float
    worst_trade_pnl: float
    avg_hold_hours: float

    # Risk metrics
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]

    # Breakdowns
    by_tier: list[TierMetrics]
    by_wallet: list[WalletMetrics]   # top 10 by total P&L
    by_category: dict[str, float]    # category → total_pnl

    # Signal funnel
    funnel: SignalFunnelStats

    # Open position summary
    open_exposure_usdc: float
    open_unrealized_pnl: float


class PerformanceTracker:
    """Computes simulation performance metrics from Postgres data."""

    def __init__(self, redis_client: object) -> None:
        self._redis = redis_client

    async def compute_metrics(self) -> SimMetrics:
        """Compute a full SimMetrics snapshot from the current DB state."""
        from config.settings import get_settings
        settings = get_settings()

        async with AsyncSessionLocal() as session:
            # All simulated positions
            all_result = await session.execute(
                select(BotPosition).where(BotPosition.is_simulated.is_(True))
            )
            all_positions = all_result.scalars().all()

        open_positions = [p for p in all_positions if p.status == PositionStatus.OPEN]
        closed_positions = [p for p in all_positions if p.status == PositionStatus.CLOSED]

        starting_bankroll = settings.SIM_BANKROLL_USDC

        # Virtual bankroll from Redis
        try:
            val = await self._redis.get("sim:bankroll")  # type: ignore[union-attr]
            virtual_bankroll = float(val) if val else starting_bankroll
        except Exception:
            virtual_bankroll = starting_bankroll

        # P&L
        realized_pnls = [p.realized_pnl_usdc or 0.0 for p in closed_positions]
        unrealized_pnls = [p.unrealized_pnl_usdc or 0.0 for p in open_positions]
        total_realized = sum(realized_pnls)
        total_unrealized = sum(unrealized_pnls)
        total_pnl = total_realized + total_unrealized
        total_return_pct = (total_pnl / starting_bankroll) * 100 if starting_bankroll > 0 else 0.0

        # Win/loss
        winners = [p for p in closed_positions if (p.realized_pnl_usdc or 0.0) > 0]
        losers = [p for p in closed_positions if (p.realized_pnl_usdc or 0.0) <= 0]
        win_count = len(winners)
        loss_count = len(losers)
        win_rate = win_count / len(closed_positions) if closed_positions else 0.0
        avg_pnl = total_realized / len(closed_positions) if closed_positions else 0.0
        best_trade = max((p.realized_pnl_usdc or 0.0 for p in closed_positions), default=0.0)
        worst_trade = min((p.realized_pnl_usdc or 0.0 for p in closed_positions), default=0.0)

        # Average hold time
        hold_hours_list = []
        for p in closed_positions:
            if p.closed_at and p.opened_at:
                opened = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=timezone.utc)
                closed = p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=timezone.utc)
                hold_hours_list.append((closed - opened).total_seconds() / 3600)
        avg_hold_hours = statistics.mean(hold_hours_list) if hold_hours_list else 0.0

        # Max drawdown — computed from cumulative P&L series
        max_drawdown_pct = _compute_max_drawdown(closed_positions, starting_bankroll)

        # Sharpe ratio (annualized, based on daily P&L)
        sharpe = _compute_sharpe(closed_positions)

        # By score tier
        by_tier = _breakdown_by_tier(closed_positions)

        # By whale wallet (top 10)
        by_wallet = _breakdown_by_wallet(closed_positions, top_n=10)

        # By category
        by_category = _breakdown_by_category(closed_positions + open_positions)

        # Signal funnel
        funnel = await _load_funnel_stats(session)

        # Open exposure
        open_exposure = sum(p.size_usdc for p in open_positions)
        open_unrealized = sum(p.unrealized_pnl_usdc or 0.0 for p in open_positions)

        return SimMetrics(
            generated_at=datetime.now(tz=timezone.utc),
            starting_bankroll=starting_bankroll,
            virtual_bankroll=virtual_bankroll,
            total_realized_pnl=round(total_realized, 4),
            total_unrealized_pnl=round(total_unrealized, 4),
            total_pnl=round(total_pnl, 4),
            total_return_pct=round(total_return_pct, 2),
            open_positions=len(open_positions),
            total_closed=len(closed_positions),
            win_count=win_count,
            loss_count=loss_count,
            win_rate=round(win_rate, 4),
            avg_pnl_per_trade=round(avg_pnl, 4),
            best_trade_pnl=round(best_trade, 4),
            worst_trade_pnl=round(worst_trade, 4),
            avg_hold_hours=round(avg_hold_hours, 2),
            max_drawdown_pct=round(max_drawdown_pct, 4),
            sharpe_ratio=round(sharpe, 3) if sharpe is not None else None,
            by_tier=by_tier,
            by_wallet=by_wallet,
            by_category=by_category,
            funnel=funnel,
            open_exposure_usdc=round(open_exposure, 2),
            open_unrealized_pnl=round(open_unrealized, 2),
        )

    def generate_report(self, metrics: SimMetrics) -> str:
        """Format a SimMetrics snapshot into a Telegram-ready string."""
        s = metrics
        sign = "+" if s.total_pnl >= 0 else ""
        pnl_emoji = "📈" if s.total_pnl >= 0 else "📉"

        lines: list[str] = [
            f"📊 <b>Sim Performance Report</b> {pnl_emoji}",
            f"<i>{s.generated_at.strftime('%Y-%m-%d %H:%M UTC')}</i>",
            "",
            "<b>P&amp;L Summary</b>",
            f"  Bankroll: ${s.starting_bankroll:,.2f} → ${s.virtual_bankroll:,.2f}",
            f"  Realized P&amp;L: {sign}${s.total_realized_pnl:,.2f}",
            f"  Unrealized P&amp;L: ${s.total_unrealized_pnl:,.2f}",
            f"  Total P&amp;L: {sign}${s.total_pnl:,.2f} ({sign}{s.total_return_pct:.2f}%)",
            "",
            "<b>Trade Stats</b>",
            f"  Closed: {s.total_closed}  |  Open: {s.open_positions}",
            f"  Win rate: {s.win_rate:.1%} ({s.win_count}W / {s.loss_count}L)",
            f"  Avg P&amp;L/trade: ${s.avg_pnl_per_trade:.2f}",
            f"  Best trade: +${s.best_trade_pnl:.2f}",
            f"  Worst trade: ${s.worst_trade_pnl:.2f}",
            f"  Avg hold: {s.avg_hold_hours:.1f}h",
            f"  Open exposure: ${s.open_exposure_usdc:,.2f}",
            "",
            "<b>Risk</b>",
            f"  Max drawdown: {s.max_drawdown_pct:.1%}",
        ]
        if s.sharpe_ratio is not None:
            lines.append(f"  Sharpe ratio: {s.sharpe_ratio:.2f}")

        # Score tier breakdown
        if s.by_tier:
            lines += ["", "<b>By Score Tier</b>"]
            for t in sorted(s.by_tier, key=lambda x: x.tier):
                sign_t = "+" if t.total_pnl >= 0 else ""
                lines.append(
                    f"  [{t.tier}] {t.total_trades} trades | "
                    f"WR {t.win_rate:.0%} | {sign_t}${t.total_pnl:.2f}"
                )

        # Category breakdown
        if s.by_category:
            lines += ["", "<b>By Category</b>"]
            for cat, pnl in sorted(s.by_category.items(), key=lambda x: x[1], reverse=True):
                sign_c = "+" if pnl >= 0 else ""
                lines.append(f"  {cat}: {sign_c}${pnl:.2f}")

        # Signal funnel
        f = s.funnel
        lines += [
            "",
            "<b>Signal Funnel</b>",
            f"  Evaluated: {f.total_evaluated} | Executed: {f.executed} ({f.execution_rate:.1%})",
            f"  Gate fails: "
            f"G1={f.skipped_gate1} G2={f.skipped_gate2} G3={f.skipped_gate3} "
            f"G4={f.skipped_gate4} G5={f.skipped_gate5} G6={f.skipped_gate6} "
            f"G7={f.skipped_gate7} G8={f.skipped_gate8}",
        ]

        # Top wallets
        if s.by_wallet:
            lines += ["", "<b>Top Whales by P&amp;L</b>"]
            for i, w in enumerate(s.by_wallet[:5], 1):
                addr = f"{w.wallet[:6]}...{w.wallet[-4:]}"
                sign_w = "+" if w.total_pnl >= 0 else ""
                lines.append(
                    f"  {i}. {addr} | {w.total_trades}t | WR {w.win_rate:.0%} | {sign_w}${w.total_pnl:.2f}"
                )

        return "\n".join(lines)

    async def persist_daily_snapshot(self, metrics: SimMetrics) -> None:
        """Upsert today's sim performance into sim_daily_snapshots."""
        from sqlalchemy import text
        date_str = metrics.generated_at.strftime("%Y-%m-%d")
        f = metrics.funnel
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT INTO sim_daily_snapshots
                            (date, virtual_bankroll, realized_pnl, unrealized_pnl, total_pnl,
                             open_positions, closed_positions, win_count, loss_count, win_rate,
                             avg_pnl_per_trade, signals_evaluated, signals_executed, signals_skipped)
                        VALUES
                            (:date, :vb, :rpnl, :upnl, :tpnl,
                             :open, :closed, :wins, :losses, :wr,
                             :avg_pnl, :eval, :exec_, :skip)
                        ON CONFLICT (date) DO UPDATE SET
                            virtual_bankroll   = EXCLUDED.virtual_bankroll,
                            realized_pnl       = EXCLUDED.realized_pnl,
                            unrealized_pnl     = EXCLUDED.unrealized_pnl,
                            total_pnl          = EXCLUDED.total_pnl,
                            open_positions     = EXCLUDED.open_positions,
                            closed_positions   = EXCLUDED.closed_positions,
                            win_count          = EXCLUDED.win_count,
                            loss_count         = EXCLUDED.loss_count,
                            win_rate           = EXCLUDED.win_rate,
                            avg_pnl_per_trade  = EXCLUDED.avg_pnl_per_trade,
                            signals_evaluated  = EXCLUDED.signals_evaluated,
                            signals_executed   = EXCLUDED.signals_executed,
                            signals_skipped    = EXCLUDED.signals_skipped
                    """),
                    {
                        "date": date_str,
                        "vb": metrics.virtual_bankroll,
                        "rpnl": metrics.total_realized_pnl,
                        "upnl": metrics.total_unrealized_pnl,
                        "tpnl": metrics.total_pnl,
                        "open": metrics.open_positions,
                        "closed": metrics.total_closed,
                        "wins": metrics.win_count,
                        "losses": metrics.loss_count,
                        "wr": metrics.win_rate,
                        "avg_pnl": metrics.avg_pnl_per_trade,
                        "eval": f.total_evaluated,
                        "exec_": f.executed,
                        "skip": f.total_evaluated - f.executed,
                    },
                )
        logger.info("performance_tracker.snapshot_persisted", date=date_str)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _compute_max_drawdown(closed: list[BotPosition], starting_bankroll: float) -> float:
    """Compute max drawdown from cumulative P&L series of closed positions."""
    if not closed:
        return 0.0
    sorted_pos = sorted(
        closed,
        key=lambda p: p.closed_at or datetime.now(tz=timezone.utc),
    )
    peak = starting_bankroll
    max_dd = 0.0
    running = starting_bankroll
    for p in sorted_pos:
        running += p.realized_pnl_usdc or 0.0
        if running > peak:
            peak = running
        elif peak > 0:
            dd = (peak - running) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _compute_sharpe(closed: list[BotPosition]) -> Optional[float]:
    """Compute annualized Sharpe ratio from daily P&L of closed positions.

    Requires at least 5 days of data for a meaningful estimate.
    """
    if len(closed) < 5:
        return None

    # Group realized P&L by calendar date
    daily: dict[str, float] = {}
    for p in closed:
        if p.closed_at is None:
            continue
        dt = p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=timezone.utc)
        date_key = dt.strftime("%Y-%m-%d")
        daily[date_key] = daily.get(date_key, 0.0) + (p.realized_pnl_usdc or 0.0)

    daily_returns = list(daily.values())
    if len(daily_returns) < 2:
        return None
    mean_daily = statistics.mean(daily_returns)
    std_daily = statistics.stdev(daily_returns)
    if std_daily == 0:
        return None
    return (mean_daily / std_daily) * math.sqrt(252)


def _breakdown_by_tier(closed: list[BotPosition]) -> list[TierMetrics]:
    buckets: dict[str, list[BotPosition]] = {}
    for p in closed:
        tier = p.score_tier or "unknown"
        buckets.setdefault(tier, []).append(p)

    result: list[TierMetrics] = []
    for tier, positions in buckets.items():
        pnls = [p.realized_pnl_usdc or 0.0 for p in positions]
        wins = sum(1 for x in pnls if x > 0)
        total = len(pnls)
        result.append(TierMetrics(
            tier=tier,
            total_trades=total,
            win_count=wins,
            loss_count=total - wins,
            total_pnl=sum(pnls),
            avg_pnl=sum(pnls) / total if total else 0.0,
            win_rate=wins / total if total else 0.0,
        ))
    return sorted(result, key=lambda x: x.tier)


def _breakdown_by_wallet(closed: list[BotPosition], top_n: int = 10) -> list[WalletMetrics]:
    buckets: dict[str, list[BotPosition]] = {}
    for p in closed:
        buckets.setdefault(p.copied_from_wallet, []).append(p)

    result: list[WalletMetrics] = []
    for wallet, positions in buckets.items():
        pnls = [p.realized_pnl_usdc or 0.0 for p in positions]
        wins = sum(1 for x in pnls if x > 0)
        total = len(pnls)
        result.append(WalletMetrics(
            wallet=wallet,
            total_trades=total,
            win_count=wins,
            total_pnl=sum(pnls),
            avg_pnl=sum(pnls) / total if total else 0.0,
            win_rate=wins / total if total else 0.0,
        ))
    return sorted(result, key=lambda x: x.total_pnl, reverse=True)[:top_n]


def _breakdown_by_category(positions: list[BotPosition]) -> dict[str, float]:
    cat_pnl: dict[str, float] = {}
    for p in positions:
        cat = p.market_category or "OTHER"
        pnl = (
            (p.realized_pnl_usdc or 0.0)
            if p.status == PositionStatus.CLOSED
            else (p.unrealized_pnl_usdc or 0.0)
        )
        cat_pnl[cat] = cat_pnl.get(cat, 0.0) + pnl
    return dict(sorted(cat_pnl.items(), key=lambda x: x[1], reverse=True))


async def _load_funnel_stats(session: object) -> SignalFunnelStats:
    """Load signal funnel statistics from the signal_events table."""
    from sqlalchemy import text

    try:
        result = await session.execute(  # type: ignore[attr-defined]
            text("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN signal_result = 'EXECUTED' THEN 1 ELSE 0 END) AS executed,
                    SUM(CASE WHEN gate_failed = 'TRADE_IS_BUY' THEN 1 ELSE 0 END)  AS g1,
                    SUM(CASE WHEN gate_failed = 'TRADE_SIZE_MIN' THEN 1 ELSE 0 END) AS g2,
                    SUM(CASE WHEN gate_failed = 'WHALE_SCORE_MIN' THEN 1 ELSE 0 END) AS g3,
                    SUM(CASE WHEN gate_failed = 'MARKET_OI_MIN' THEN 1 ELSE 0 END) AS g4,
                    SUM(CASE WHEN gate_failed = 'ORDERBOOK_DEPTH' THEN 1 ELSE 0 END) AS g5,
                    SUM(CASE WHEN gate_failed = 'POSITION_CAP' THEN 1 ELSE 0 END) AS g6,
                    SUM(CASE WHEN gate_failed = 'TIME_TO_RESOLUTION' THEN 1 ELSE 0 END) AS g7,
                    SUM(CASE WHEN gate_failed = 'CIRCUIT_BREAKER' THEN 1 ELSE 0 END) AS g8,
                    SUM(CASE WHEN signal_result = 'RISK_REJECTED' THEN 1 ELSE 0 END) AS risk_rej
                FROM signal_events
            """)
        )
        row = result.fetchone()
        if row is None or row[0] == 0:
            return _empty_funnel()
        total = int(row[0] or 0)
        executed = int(row[1] or 0)
        return SignalFunnelStats(
            total_evaluated=total,
            executed=executed,
            skipped_gate1=int(row[2] or 0),
            skipped_gate2=int(row[3] or 0),
            skipped_gate3=int(row[4] or 0),
            skipped_gate4=int(row[5] or 0),
            skipped_gate5=int(row[6] or 0),
            skipped_gate6=int(row[7] or 0),
            skipped_gate7=int(row[8] or 0),
            skipped_gate8=int(row[9] or 0),
            risk_rejected=int(row[10] or 0),
            execution_rate=executed / total if total > 0 else 0.0,
        )
    except Exception as exc:
        logger.warning("performance_tracker.funnel_query_failed", error=str(exc))
        return _empty_funnel()


def _empty_funnel() -> SignalFunnelStats:
    return SignalFunnelStats(
        total_evaluated=0, executed=0,
        skipped_gate1=0, skipped_gate2=0, skipped_gate3=0, skipped_gate4=0,
        skipped_gate5=0, skipped_gate6=0, skipped_gate7=0, skipped_gate8=0,
        risk_rejected=0, execution_rate=0.0,
    )
