"""Order placement, monitoring, and lifecycle management.

The executor places limit orders via the CLOB API and manages their lifecycle:
  PENDING → FILLED (happy path)
  PENDING → EXPIRED (after ORDER_FILL_TIMEOUT_SECONDS)
  PENDING → CANCELLED (manual / risk-gate cancellation)

All state changes are persisted transactionally in Postgres.
A background asyncio task monitors open orders every 15 seconds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog
from pydantic import BaseModel
from sqlalchemy import select

from config.settings import get_settings
from data.clob_client import CLOBClient, Market
from db.models import BotOrder
from db.models import BotPosition as BotPositionModel
from db.models import OrderStatus as OrderStatusEnum
from db.models import PositionStatus, SideEnum
from db.session import AsyncSessionLocal
from signals.signal_engine import SignalDecision

logger = structlog.get_logger(__name__)

_settings = get_settings()


class ExecutionResult(BaseModel):
    success: bool
    order_id: Optional[str] = None
    position_id: Optional[int] = None
    fill_price: Optional[float] = None
    reason: str = ""


class OrderExecutor:
    """Places and monitors CLOB orders for copy-trade signals."""

    def __init__(
        self,
        clob_client: CLOBClient,
        alerter: object,  # TelegramAlerter
    ) -> None:
        self._clob = clob_client
        self._alerter = alerter
        self._settings = get_settings()
        self._monitor_task: Optional[asyncio.Task[None]] = None

    async def execute(
        self,
        signal: SignalDecision,
        market: Market,
        copied_from_wallet: str,
    ) -> ExecutionResult:
        """Place a limit order and persist the BotPosition + BotOrder records.

        Returns immediately after placing the order. Fill monitoring happens
        asynchronously in the background.
        """
        if not market.tokens:
            return ExecutionResult(success=False, reason="No tokens available for market")

        # Use the first YES token if buying
        token = market.tokens[0]
        token_id = token.token_id
        entry_price = signal.copy_size_usdc / (signal.copy_size_usdc / 0.5)  # midpoint estimate
        try:
            orderbook = await self._clob.get_orderbook(token_id)
            entry_price = orderbook.asks[0].price if orderbook.asks else 0.5
        except Exception:
            pass  # Use midpoint estimate

        shares = signal.copy_size_usdc / entry_price if entry_price > 0 else 0.0

        # Place the order on the CLOB
        try:
            order_result = await self._clob.place_limit_order(
                token_id=token_id,
                side="BUY",
                price=entry_price,
                size=shares,
            )
        except Exception as exc:
            logger.error(
                "executor.place_order_failed",
                market=market.market_id,
                error=str(exc),
            )
            return ExecutionResult(success=False, reason=f"Order placement failed: {exc}")

        now = datetime.now(tz=timezone.utc)

        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Create BotPosition record
                position = BotPositionModel(
                    market_id=market.market_id,
                    token_id=token_id,
                    side=SideEnum.BUY,
                    entry_price=entry_price,
                    size_usdc=signal.copy_size_usdc,
                    shares_held=shares,
                    copied_from_wallet=copied_from_wallet,
                    whale_score_at_entry=signal.whale_score,
                    status=PositionStatus.OPEN,
                    opened_at=now,
                )
                session.add(position)
                await session.flush()  # Get position.id

                # Create BotOrder record
                bot_order = BotOrder(
                    bot_position_id=position.id,
                    clob_order_id=order_result.order_id,
                    market_id=market.market_id,
                    token_id=token_id,
                    side=SideEnum.BUY,
                    limit_price=entry_price,
                    size_usdc=signal.copy_size_usdc,
                    status=OrderStatusEnum.PENDING,
                    placed_at=now,
                )
                session.add(bot_order)
                position_id = position.id

        logger.info(
            "executor.order_placed",
            order_id=order_result.order_id,
            position_id=position_id,
            market=market.market_id,
            size_usdc=signal.copy_size_usdc,
            price=entry_price,
        )

        # Alert
        try:
            await self._alerter.trade_executed(  # type: ignore[attr-defined]
                market=market.question,
                side="BUY",
                size=signal.copy_size_usdc,
                price=entry_price,
                copy_of=copied_from_wallet,
            )
        except Exception as exc:
            logger.warning("executor.alert_failed", error=str(exc))

        # Kick off timeout monitor as a non-blocking background task
        asyncio.create_task(
            self._monitor_single_order(
                order_id=order_result.order_id,
                position_id=position_id,
                market_question=market.question,
                size_usdc=signal.copy_size_usdc,
            )
        )

        return ExecutionResult(
            success=True,
            order_id=order_result.order_id,
            position_id=position_id,
        )

    async def monitor_open_orders(self) -> None:
        """Poll all PENDING orders and update their status in Postgres.

        Called every 15 seconds by the APScheduler job.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotOrder).where(BotOrder.status == OrderStatusEnum.PENDING)
            )
            pending_orders = result.scalars().all()

        if not pending_orders:
            return

        logger.debug("executor.monitor.polling", count=len(pending_orders))

        for order in pending_orders:
            try:
                status = await self._clob.get_order_status(order.clob_order_id)
            except Exception as exc:
                logger.warning(
                    "executor.monitor.poll_failed",
                    order_id=order.clob_order_id,
                    error=str(exc),
                )
                continue

            if status.status == "FILLED":
                await self._mark_order_filled(order.id, order.bot_position_id, status.fill_price or order.limit_price)
            elif status.status in ("CANCELLED", "EXPIRED"):
                await self._mark_order_cancelled(order.id, order.bot_position_id, status.status)

    async def cancel_stale_orders(self) -> None:
        """Cancel any PENDING orders older than ORDER_FILL_TIMEOUT_SECONDS."""
        now = datetime.now(tz=timezone.utc)
        timeout = self._settings.ORDER_FILL_TIMEOUT_SECONDS

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotOrder).where(BotOrder.status == OrderStatusEnum.PENDING)
            )
            pending = result.scalars().all()

        for order in pending:
            placed_at = order.placed_at
            if placed_at.tzinfo is None:
                placed_at = placed_at.replace(tzinfo=timezone.utc)
            age_seconds = (now - placed_at).total_seconds()
            if age_seconds >= timeout:
                await self._expire_order(
                    order_id=order.clob_order_id,
                    db_order_id=order.id,
                    position_id=order.bot_position_id,
                    market_id=order.market_id,
                    size_usdc=order.size_usdc,
                )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _monitor_single_order(
        self,
        order_id: str,
        position_id: int,
        market_question: str,
        size_usdc: float,
    ) -> None:
        """Background task: poll a single order until filled or timed out."""
        timeout = self._settings.ORDER_FILL_TIMEOUT_SECONDS
        poll_interval = 10
        elapsed = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status = await self._clob.get_order_status(order_id)
            except Exception as exc:
                logger.warning("executor.monitor_task.poll_error", order_id=order_id, error=str(exc))
                continue

            if status.status == "FILLED":
                fill_price = status.fill_price or 0.0
                await self._mark_order_filled(None, position_id, fill_price, order_id=order_id)
                try:
                    await self._alerter.order_filled(  # type: ignore[attr-defined]
                        market=market_question, fill_price=fill_price, size=size_usdc
                    )
                except Exception:
                    pass
                return

            if status.status in ("CANCELLED", "EXPIRED"):
                await self._mark_order_cancelled(None, position_id, status.status, order_id=order_id)
                return

        # Timeout reached
        await self._expire_order(
            order_id=order_id,
            db_order_id=None,
            position_id=position_id,
            market_id="",
            size_usdc=size_usdc,
            market_question=market_question,
        )

    async def _mark_order_filled(
        self,
        db_order_id: Optional[int],
        position_id: int,
        fill_price: float,
        order_id: Optional[str] = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if db_order_id is not None:
                    result = await session.get(BotOrder, db_order_id)
                    order = result
                elif order_id is not None:
                    r = await session.execute(
                        select(BotOrder).where(BotOrder.clob_order_id == order_id)
                    )
                    order = r.scalar_one_or_none()
                else:
                    order = None

                if order:
                    order.status = OrderStatusEnum.FILLED
                    order.filled_at = now
                    order.fill_price = fill_price

                position = await session.get(BotPositionModel, position_id)
                if position:
                    position.entry_price = fill_price

        logger.info("executor.order_filled", position_id=position_id, fill_price=fill_price)

    async def _mark_order_cancelled(
        self,
        db_order_id: Optional[int],
        position_id: int,
        reason: str,
        order_id: Optional[str] = None,
    ) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if db_order_id is not None:
                    order = await session.get(BotOrder, db_order_id)
                elif order_id is not None:
                    r = await session.execute(
                        select(BotOrder).where(BotOrder.clob_order_id == order_id)
                    )
                    order = r.scalar_one_or_none()
                else:
                    order = None

                if order:
                    order.status = (
                        OrderStatusEnum.CANCELLED
                        if reason == "CANCELLED"
                        else OrderStatusEnum.EXPIRED
                    )

                position = await session.get(BotPositionModel, position_id)
                if position:
                    position.status = PositionStatus.CANCELLED

    async def _expire_order(
        self,
        order_id: str,
        db_order_id: Optional[int],
        position_id: int,
        market_id: str,
        size_usdc: float,
        market_question: str = "",
    ) -> None:
        """Cancel a stale order on the CLOB and mark it EXPIRED in Postgres."""
        cancelled = await self._clob.cancel_order(order_id)
        logger.info(
            "executor.order_expired",
            order_id=order_id,
            position_id=position_id,
            cancelled_on_clob=cancelled,
        )

        async with AsyncSessionLocal() as session:
            async with session.begin():
                if db_order_id is not None:
                    order = await session.get(BotOrder, db_order_id)
                else:
                    r = await session.execute(
                        select(BotOrder).where(BotOrder.clob_order_id == order_id)
                    )
                    order = r.scalar_one_or_none()

                if order:
                    order.status = OrderStatusEnum.EXPIRED

                position = await session.get(BotPositionModel, position_id)
                if position:
                    position.status = PositionStatus.CANCELLED
                    position.exit_reason = "ORDER_EXPIRED"
                    position.closed_at = datetime.now(tz=timezone.utc)

        try:
            await self._alerter.order_expired(  # type: ignore[attr-defined]
                market=market_question or market_id, size=size_usdc
            )
        except Exception as exc:
            logger.warning("executor.expire_alert_failed", error=str(exc))
