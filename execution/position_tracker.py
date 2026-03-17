"""Tracks and manages the bot's open positions.

Provides helpers for:
  - Querying open positions from Postgres
  - Computing unrealized P&L (marks to current orderbook mid)
  - Closing positions (either manually or on market resolution)
  - Syncing bankroll from realized P&L + current capital
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select, func

from config.settings import get_settings
from data.clob_client import CLOBClient
from db.models import BotOrder, BotPosition, OrderStatus, PositionStatus, SideEnum
from db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

_settings = get_settings()


class PositionTracker:
    """Queries and updates the bot's own positions in Postgres."""

    def __init__(self, clob_client: CLOBClient, redis_client: object) -> None:
        self._clob = clob_client
        self._redis = redis_client
        self._settings = get_settings()

    async def get_open_positions(self) -> list[BotPosition]:
        """Return all positions with status OPEN."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotPosition).where(BotPosition.status == PositionStatus.OPEN)
            )
            return list(result.scalars().all())

    async def get_position_by_id(self, position_id: int) -> Optional[BotPosition]:
        async with AsyncSessionLocal() as session:
            return await session.get(BotPosition, position_id)

    async def compute_unrealized_pnl(self) -> float:
        """Compute aggregate unrealized P&L across all open positions."""
        open_positions = await self.get_open_positions()
        if not open_positions:
            return 0.0

        total_unrealized = 0.0
        for pos in open_positions:
            try:
                orderbook = await self._clob.get_orderbook(pos.token_id)
                mid = orderbook.mid_price
                if pos.side == SideEnum.BUY:
                    pnl = (mid - pos.entry_price) * pos.shares_held
                else:
                    pnl = (pos.entry_price - mid) * pos.shares_held
                total_unrealized += pnl
            except Exception as exc:
                logger.warning(
                    "position_tracker.unrealized_pnl_error",
                    position_id=pos.id,
                    error=str(exc),
                )
        return total_unrealized

    async def close_position(
        self,
        position_id: int,
        exit_price: float,
        reason: str = "MANUAL",
    ) -> float:
        """Mark a position as CLOSED and compute realized P&L.

        Returns the realized P&L in USDC.
        """
        async with AsyncSessionLocal() as session:
            async with session.begin():
                position = await session.get(BotPosition, position_id)
                if position is None:
                    raise ValueError(f"Position {position_id} not found")
                if position.status != PositionStatus.OPEN:
                    raise ValueError(f"Position {position_id} is not OPEN (status={position.status})")

                if position.side == SideEnum.BUY:
                    realized_pnl = (exit_price - position.entry_price) * position.shares_held
                else:
                    realized_pnl = (position.entry_price - exit_price) * position.shares_held

                position.status = PositionStatus.CLOSED
                position.closed_at = datetime.now(tz=timezone.utc)
                position.realized_pnl_usdc = realized_pnl
                position.exit_reason = reason

        logger.info(
            "position_tracker.closed",
            position_id=position_id,
            realized_pnl=realized_pnl,
            reason=reason,
        )
        return realized_pnl

    async def sync_bankroll(self) -> float:
        """Recompute the bot's effective bankroll and persist to Redis.

        bankroll = initial_bankroll + sum(realized_pnl) over all time

        Returns the new bankroll value.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(func.sum(BotPosition.realized_pnl_usdc)).where(
                    BotPosition.status == PositionStatus.CLOSED,
                    BotPosition.realized_pnl_usdc.is_not(None),
                )
            )
            total_realized = float(result.scalar_one_or_none() or 0.0)

            # Count capital tied up in open positions
            open_result = await session.execute(
                select(func.sum(BotPosition.size_usdc)).where(
                    BotPosition.status == PositionStatus.OPEN
                )
            )
            open_exposure = float(open_result.scalar_one_or_none() or 0.0)

        initial = self._settings.BANKROLL_USDC
        # Effective bankroll = initial + all realized P&L (free capital)
        effective_bankroll = initial + total_realized

        try:
            await self._redis.set("bot:bankroll", str(effective_bankroll))  # type: ignore[union-attr]
            # Update peak
            peak_raw = await self._redis.get("bot:peak_bankroll")  # type: ignore[union-attr]
            peak = float(peak_raw) if peak_raw else 0.0
            if effective_bankroll > peak:
                await self._redis.set("bot:peak_bankroll", str(effective_bankroll))  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("position_tracker.bankroll_sync_failed", error=str(exc))

        logger.info(
            "position_tracker.bankroll_synced",
            bankroll=effective_bankroll,
            realized_pnl=total_realized,
            open_exposure=open_exposure,
        )
        return effective_bankroll

    async def get_daily_stats(self, date_str: str) -> dict[str, float]:
        """Aggregate trade stats for a given date (YYYY-MM-DD)."""
        from datetime import date
        target_date = date.fromisoformat(date_str)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotPosition).where(
                    BotPosition.status == PositionStatus.CLOSED,
                    func.date(BotPosition.closed_at) == target_date,
                )
            )
            closed = result.scalars().all()

        realized_pnl = sum(p.realized_pnl_usdc or 0.0 for p in closed)
        trade_count = len(closed)
        win_count = sum(1 for p in closed if (p.realized_pnl_usdc or 0.0) > 0)
        loss_count = trade_count - win_count

        return {
            "realized_pnl": realized_pnl,
            "trade_count": trade_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_count / trade_count if trade_count > 0 else 0.0,
        }
