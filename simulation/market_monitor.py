"""Market monitor for paper-trading simulation.

Periodically:
  1. Fetches live orderbook mid-prices for all open paper positions.
  2. Updates current_price and unrealized_pnl_usdc in Postgres.
  3. Detects resolved markets (active=False) and auto-closes positions at
     their resolution price (1.0 for win, 0.0 for loss).

This provides the real-time performance view the user needs to assess the
simulated strategy without placing any live orders.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select, update

from config.settings import get_settings
from data.clob_client import CLOBClient, Market
from db.models import BotPosition, PositionStatus, SideEnum
from db.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

_settings = get_settings()


class MarketMonitor:
    """Monitors open paper positions: mark-to-market and auto-resolution.

    Args:
        clob_client: Shared CLOBClient for market and orderbook fetches.
        alerter: TelegramAlerter for position-closed alerts (optional).
        paper_trader: PaperTrader used to restore bankroll on position close.
    """

    def __init__(
        self,
        clob_client: CLOBClient,
        alerter: object,
        paper_trader: object,
        redis_client: object = None,
    ) -> None:
        self._clob = clob_client
        self._alerter = alerter
        self._paper_trader = paper_trader
        self._redis = redis_client
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def mark_to_market(self) -> int:
        """Update unrealized P&L for all open paper positions.

        Fetches the live best-bid price for each token and updates
        current_price and unrealized_pnl_usdc in Postgres.

        Returns:
            Number of positions successfully marked.
        """
        positions = await self._fetch_open_positions()
        if not positions:
            logger.debug("market_monitor.mark_to_market.no_open_positions")
            return 0

        marked = 0
        now = datetime.now(tz=timezone.utc)

        for pos in positions:
            try:
                current_price = await self._get_mid_price(pos.token_id)
                if current_price is None:
                    continue

                # Unrealized P&L = (current_price - entry_price) * shares_held
                unrealized_pnl = (current_price - pos.entry_price) * pos.shares_held

                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        await session.execute(
                            update(BotPosition)
                            .where(BotPosition.id == pos.id)
                            .values(
                                current_price=current_price,
                                unrealized_pnl_usdc=unrealized_pnl,
                                last_marked_at=now,
                            )
                        )

                marked += 1
                logger.debug(
                    "market_monitor.marked",
                    position_id=pos.id,
                    market=pos.market_id,
                    current_price=round(current_price, 4),
                    unrealized_pnl=round(unrealized_pnl, 2),
                )

                # Strategy-specific and generic exit checks
                if pos.size_usdc > 0:
                    pnl_pct = unrealized_pnl / pos.size_usdc

                    # NO_FLIP has three dedicated exit conditions
                    if getattr(pos, "strategy", "COPY") == "NO_FLIP":
                        no_flip_exit = await self._check_no_flip_exit(pos, current_price, pnl_pct)
                        if no_flip_exit:
                            await self._force_close_position(pos, current_price, no_flip_exit)
                            continue
                    else:
                        stop_loss = -self._settings.SIM_STOP_LOSS_PCT
                        take_profit = self._settings.SIM_TAKE_PROFIT_PCT
                        if pnl_pct <= stop_loss:
                            await self._force_close_position(pos, current_price, "STOP_LOSS")
                            continue
                        elif pnl_pct >= take_profit:
                            await self._force_close_position(pos, current_price, "TAKE_PROFIT")
                            continue

                # Time-based exit: close at 50% of elapsed position window if profitable.
                # Resolution time is cached in Redis when the position is opened.
                if self._redis is not None:
                    try:
                        rt_raw = await self._redis.get(f"pos:{pos.id}:resolution_time")  # type: ignore[union-attr]
                        if rt_raw:
                            rt_str = rt_raw.decode() if isinstance(rt_raw, bytes) else rt_raw
                            from datetime import timezone as _tz
                            rt = datetime.fromisoformat(rt_str)
                            if rt.tzinfo is None:
                                rt = rt.replace(tzinfo=timezone.utc)
                            opened = pos.opened_at
                            if opened.tzinfo is None:
                                opened = opened.replace(tzinfo=timezone.utc)
                            total_secs = (rt - opened).total_seconds()
                            elapsed_secs = (now - opened).total_seconds()
                            if total_secs > 0 and elapsed_secs / total_secs >= 0.5:
                                if unrealized_pnl > 0:
                                    logger.info(
                                        "market_monitor.time_exit_triggered",
                                        position_id=pos.id,
                                        elapsed_pct=round(elapsed_secs / total_secs * 100, 1),
                                        unrealized_pnl=round(unrealized_pnl, 2),
                                    )
                                    await self._force_close_position(pos, current_price, "TIME_EXIT")
                                    continue
                    except Exception as exc:
                        logger.debug("market_monitor.time_exit_check_failed", pos_id=pos.id, error=str(exc))

            except Exception as exc:
                logger.warning(
                    "market_monitor.mark_failed",
                    position_id=pos.id,
                    market=pos.market_id,
                    error=str(exc),
                )

        # Log portfolio snapshot alongside mark
        try:
            portfolio = await self._paper_trader.get_portfolio_value()  # type: ignore[attr-defined]
            logger.info(
                "market_monitor.mark_to_market.complete",
                marked=marked,
                total=len(positions),
                available_cash=portfolio["available_cash"],
                deployed_capital=portfolio["deployed_capital"],
                unrealized_pnl=portfolio["unrealized_pnl"],
                portfolio_value=portfolio["portfolio_value"],
            )
        except Exception:
            logger.info("market_monitor.mark_to_market.complete", marked=marked, total=len(positions))
        return marked

    async def check_resolutions(self) -> int:
        """Auto-close positions where the market has resolved.

        Checks each open position's market via the CLOB API. When a market
        is no longer active (active=False), the outcome is determined and the
        position is closed at the resolution price (1.0 win / 0.0 loss).

        Returns:
            Number of positions auto-closed.
        """
        if not self._settings.SIM_AUTO_CLOSE_ON_RESOLUTION:
            return 0

        positions = await self._fetch_open_positions()
        if not positions:
            return 0

        closed = 0

        for pos in positions:
            try:
                market = await self._clob.get_market(pos.market_id)

                if market.active:
                    # Still running — nothing to do
                    continue

                # Market resolved — determine P&L
                await self._close_resolved_position(pos, market)
                closed += 1

            except Exception as exc:
                logger.warning(
                    "market_monitor.resolution_check_failed",
                    position_id=pos.id,
                    market=pos.market_id,
                    error=str(exc),
                )

        if closed:
            logger.info("market_monitor.resolutions_closed", count=closed)
        return closed

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_open_positions(self) -> list[BotPosition]:
        """Return all open simulated positions from Postgres."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotPosition).where(
                    BotPosition.status == PositionStatus.OPEN,
                    BotPosition.is_simulated.is_(True),
                )
            )
            return list(result.scalars().all())

    async def _get_mid_price(self, token_id: str) -> Optional[float]:
        """Fetch orderbook and return the mid price (best_bid + best_ask) / 2.

        Falls back to best_bid alone if there are no asks.
        Returns None if the orderbook is completely empty.
        """
        try:
            book = await self._clob.get_orderbook(token_id)
            best_bid = book.bids[0].price if book.bids else None
            best_ask = book.asks[0].price if book.asks else None

            if best_bid is not None and best_ask is not None:
                return (best_bid + best_ask) / 2.0
            elif best_bid is not None:
                return best_bid
            elif best_ask is not None:
                return best_ask
            return None
        except Exception as exc:
            logger.debug("market_monitor.orderbook_failed", token=token_id, error=str(exc))
            return None

    async def _close_resolved_position(
        self,
        pos: BotPosition,
        market: Market,
    ) -> None:
        """Determine resolution price and close the paper position.

        Resolution logic:
        - Fetch the current token price from the orderbook. If the market
          resolved YES and the position holds YES tokens, price → 1.0.
          If it resolved NO, price → 0.0.
        - Since we don't have structured resolution data from CLOB REST,
          we use the best mid-price and clamp: if price > 0.7 → 1.0 (win),
          else → 0.0 (loss).  This is a heuristic; real resolution data
          would be more accurate.
        """
        # Try to get the resolution price
        mid = await self._get_mid_price(pos.token_id)
        if mid is None:
            # Use the last known price as proxy
            mid = pos.current_price if pos.current_price else pos.entry_price

        # Clamp to binary outcome based on post-resolution orderbook
        exit_price = 1.0 if (mid is not None and mid >= 0.70) else 0.0
        realized_pnl = (exit_price - pos.entry_price) * pos.shares_held
        exit_reason = "RESOLUTION_WIN" if exit_price >= 1.0 else "RESOLUTION_LOSS"

        # Proceeds = what we get back (exit_price * shares).  This is the
        # amount to restore to the liquid bankroll.
        proceeds = exit_price * pos.shares_held

        now = datetime.now(tz=timezone.utc)

        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(BotPosition)
                    .where(BotPosition.id == pos.id)
                    .values(
                        status=PositionStatus.CLOSED,
                        exit_price=exit_price,
                        current_price=exit_price,
                        unrealized_pnl_usdc=0.0,
                        realized_pnl_usdc=realized_pnl,
                        closed_at=now,
                        exit_reason=exit_reason,
                    )
                )

        # Restore proceeds to liquid cash
        try:
            await self._paper_trader.restore_bankroll(proceeds)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("market_monitor.bankroll_restore_failed", error=str(exc))

        pnl_sign = "+" if realized_pnl >= 0 else ""
        logger.info(
            "market_monitor.position_auto_closed",
            position_id=pos.id,
            market=pos.market_id,
            question=pos.market_question,
            exit_price=exit_price,
            realized_pnl=round(realized_pnl, 2),
            reason=exit_reason,
        )

        # Telegram alert
        try:
            await self._alerter.sim_position_closed(  # type: ignore[attr-defined]
                market=pos.market_question or pos.market_id,
                pnl=realized_pnl,
                exit_reason=exit_reason,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                size=pos.size_usdc,
            )
        except Exception as exc:
            logger.debug("market_monitor.close_alert_failed", error=str(exc))

    async def _force_close_position(
        self,
        pos: BotPosition,
        exit_price: float,
        reason: str,
    ) -> None:
        """Close a position at the current market price (stop-loss or take-profit)."""
        realized_pnl = (exit_price - pos.entry_price) * pos.shares_held
        proceeds = exit_price * pos.shares_held
        now = datetime.now(tz=timezone.utc)

        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(BotPosition)
                    .where(BotPosition.id == pos.id)
                    .values(
                        status=PositionStatus.CLOSED,
                        exit_price=exit_price,
                        current_price=exit_price,
                        unrealized_pnl_usdc=0.0,
                        realized_pnl_usdc=realized_pnl,
                        closed_at=now,
                        exit_reason=reason,
                    )
                )

        try:
            await self._paper_trader.restore_bankroll(proceeds)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("market_monitor.stop_loss_bankroll_restore_failed", error=str(exc))

        pnl_sign = "+" if realized_pnl >= 0 else ""
        logger.info(
            "market_monitor.position_force_closed",
            position_id=pos.id,
            market=pos.market_id,
            reason=reason,
            exit_price=round(exit_price, 4),
            realized_pnl=f"{pnl_sign}{round(realized_pnl, 2)}",
            pnl_pct=f"{pnl_sign}{round(realized_pnl / pos.size_usdc * 100, 1)}%",
        )

        try:
            await self._alerter.sim_position_closed(  # type: ignore[attr-defined]
                market=pos.market_question or pos.market_id,
                pnl=realized_pnl,
                exit_reason=reason,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                size=pos.size_usdc,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Fast exit check (60-second cadence for volatile positions)
    # ------------------------------------------------------------------

    async def fast_exit_check(self) -> int:
        """Quick pass over volatile open positions — triggers exits without full mark.

        Only re-prices positions with |unrealized_pnl_pct| >= 10% so the
        60-second scheduler loop stays lightweight.

        Returns number of positions force-closed.
        """
        positions = await self._fetch_open_positions()
        closed = 0
        for pos in positions:
            if pos.size_usdc <= 0:
                continue
            # Only run fast check for positions that are already moving
            last_pnl = pos.unrealized_pnl_usdc or 0.0
            if abs(last_pnl / pos.size_usdc) < 0.10 and getattr(pos, "strategy", "COPY") != "NO_FLIP":
                continue
            try:
                current_price = await self._get_mid_price(pos.token_id)
                if current_price is None:
                    continue
                unrealized_pnl = (current_price - pos.entry_price) * pos.shares_held
                pnl_pct = unrealized_pnl / pos.size_usdc

                strategy = getattr(pos, "strategy", "COPY")
                if strategy == "NO_FLIP":
                    reason = await self._check_no_flip_exit(pos, current_price, pnl_pct)
                    if reason:
                        await self._force_close_position(pos, current_price, reason)
                        closed += 1
                        continue
                else:
                    if pnl_pct <= -self._settings.SIM_STOP_LOSS_PCT:
                        await self._force_close_position(pos, current_price, "STOP_LOSS")
                        closed += 1
                    elif pnl_pct >= self._settings.SIM_TAKE_PROFIT_PCT:
                        await self._force_close_position(pos, current_price, "TAKE_PROFIT")
                        closed += 1
            except Exception as exc:
                logger.debug("market_monitor.fast_exit_check_failed", pos_id=pos.id, error=str(exc))

        if closed:
            logger.info("market_monitor.fast_exit_check.closed", count=closed)
        return closed

    async def _check_no_flip_exit(
        self,
        pos: BotPosition,
        no_price: float,
        pnl_pct: float,
    ) -> Optional[str]:
        """Return an exit reason string if a NO_FLIP position should be closed, else None.

        Exit conditions (first match wins):
          1. NO_FLIP_TAKE_PROFIT_PCT (+50%) — NO token moved in our favour.
          2. Absolute stop-loss — NO price dropped below NO_FLIP_STOP_LOSS_PRICE ($0.02).
          3. YES_REVERSION — YES price fell back from >0.90 to <NO_FLIP_YES_REVERSION_THRESHOLD.
             This is the mean-reversion signal: the whale's price push has reversed.
        """
        settings = self._settings
        # 1. Take profit
        if pnl_pct >= settings.NO_FLIP_TAKE_PROFIT_PCT:
            return "NO_FLIP_TAKE_PROFIT"

        # 2. Absolute stop loss
        if no_price <= settings.NO_FLIP_STOP_LOSS_PRICE:
            return "NO_FLIP_STOP_LOSS"

        # 3. YES reversion — only if we can fetch the YES price
        yes_price = await self._get_yes_price_for_no_position(pos)
        if yes_price is not None and yes_price < settings.NO_FLIP_YES_REVERSION_THRESHOLD:
            logger.info(
                "market_monitor.no_flip_yes_reversion",
                position_id=pos.id,
                yes_price=round(yes_price, 4),
                threshold=settings.NO_FLIP_YES_REVERSION_THRESHOLD,
            )
            return "NO_FLIP_YES_REVERSION"

        return None

    async def _get_yes_price_for_no_position(self, pos: BotPosition) -> Optional[float]:
        """Return the current mid-price of the paired YES token for a NO_FLIP position.

        Fetches the market and finds the token whose token_id differs from pos.token_id.
        Returns None if the market can't be fetched or has only one token.
        """
        try:
            market = await self._clob.get_market(pos.market_id)
            if not market.tokens or len(market.tokens) < 2:
                return None
            yes_token = next(
                (t for t in market.tokens if t.token_id != pos.token_id), None
            )
            if yes_token is None:
                return None
            return await self._get_mid_price(yes_token.token_id)
        except Exception as exc:
            logger.debug("market_monitor.yes_price_lookup_failed", pos_id=pos.id, error=str(exc))
            return None
