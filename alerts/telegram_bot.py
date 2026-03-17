"""Telegram alert dispatcher using python-telegram-bot v20+ async API."""

from __future__ import annotations

from typing import Optional

import structlog
from telegram import Bot
from telegram.error import TelegramError

from config.settings import get_settings

logger = structlog.get_logger(__name__)

_settings = get_settings()


class TelegramAlerter:
    """Sends formatted Telegram messages for all bot lifecycle events."""

    def __init__(self) -> None:
        self._settings = get_settings()
        token = self._settings.TELEGRAM_BOT_TOKEN
        self._bot: Optional[Bot] = Bot(token=token) if token else None
        self._chat_id = self._settings.TELEGRAM_CHAT_ID

    async def _send(self, text: str) -> None:
        """Send a message, swallowing errors to avoid blocking the main loop."""
        if self._bot is None or not self._chat_id:
            logger.debug("telegram.disabled", text=text[:120])
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.error("telegram.send_failed", error=str(exc), text=text[:100])
        except Exception as exc:
            logger.error("telegram.unexpected_error", error=str(exc))

    # ------------------------------------------------------------------
    # Alert methods
    # ------------------------------------------------------------------

    async def whale_entry_detected(
        self,
        wallet: str,
        market: str,
        side: str,
        size: float,
        price: float,
        score: float,
    ) -> None:
        short_wallet = f"{wallet[:6]}...{wallet[-4:]}"
        text = (
            f"🐋 <b>Whale Entry Detected</b>\n"
            f"Wallet: <code>{short_wallet}</code> (score: {score:.1f})\n"
            f"Market: {market}\n"
            f"Side: {side} @ ${price:.4f}\n"
            f"Size: ${size:,.2f}"
        )
        await self._send(text)
        logger.info("telegram.whale_entry_sent", wallet=wallet, market=market)

    async def trade_executed(
        self,
        market: str,
        side: str,
        size: float,
        price: float,
        copy_of: str,
    ) -> None:
        short_wallet = f"{copy_of[:6]}...{copy_of[-4:]}"
        text = (
            f"✅ <b>Trade Executed</b>\n"
            f"Market: {market}\n"
            f"Side: {side} @ ${price:.4f}\n"
            f"Size: ${size:,.2f}\n"
            f"Copying: <code>{short_wallet}</code>"
        )
        await self._send(text)

    async def trade_skipped(
        self,
        market: str,
        reason: str,
        whale_wallet: str,
    ) -> None:
        short_wallet = f"{whale_wallet[:6]}...{whale_wallet[-4:]}"
        text = (
            f"⏭ <b>Trade Skipped</b>\n"
            f"Market: {market}\n"
            f"Wallet: <code>{short_wallet}</code>\n"
            f"Reason: {reason}"
        )
        await self._send(text)

    async def order_filled(
        self,
        market: str,
        fill_price: float,
        size: float,
    ) -> None:
        text = (
            f"💰 <b>Order Filled</b>\n"
            f"Market: {market}\n"
            f"Fill price: ${fill_price:.4f}\n"
            f"Size: ${size:,.2f}"
        )
        await self._send(text)

    async def order_expired(self, market: str, size: float) -> None:
        text = (
            f"⏱ <b>Order Expired Unfilled</b>\n"
            f"Market: {market}\n"
            f"Size: ${size:,.2f}\n"
            f"Action: order cancelled, position voided"
        )
        await self._send(text)

    async def position_closed(
        self,
        market: str,
        pnl: float,
        exit_reason: str,
    ) -> None:
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        sign = "+" if pnl >= 0 else ""
        text = (
            f"📊 <b>Position Closed</b> {pnl_emoji}\n"
            f"Market: {market}\n"
            f"Realized P&amp;L: {sign}${pnl:,.2f}\n"
            f"Reason: {exit_reason}"
        )
        await self._send(text)

    async def circuit_breaker_triggered(
        self,
        current_bankroll: float,
        drawdown_pct: float,
    ) -> None:
        text = (
            f"🚨 <b>CIRCUIT BREAKER TRIGGERED</b> 🚨\n"
            f"Current bankroll: ${current_bankroll:,.2f}\n"
            f"Drawdown: {drawdown_pct:.1%}\n"
            f"⛔ All trading halted. Manual reset required.\n"
            f"Reset with: <code>redis-cli SET bot:circuit_breaker_active 0</code>"
        )
        await self._send(text)
        logger.critical(
            "telegram.circuit_breaker_alert_sent",
            bankroll=current_bankroll,
            drawdown=drawdown_pct,
        )

    async def whitelist_refreshed(
        self,
        added: int,
        removed: int,
        retained: int,
    ) -> None:
        text = (
            f"🔄 <b>Whitelist Refreshed</b>\n"
            f"Added: {added} wallets\n"
            f"Removed: {removed} wallets\n"
            f"Retained: {retained} wallets\n"
            f"Total: {added + retained} active whale wallets"
        )
        await self._send(text)

    async def daily_summary(
        self,
        pnl: float,
        win_rate: float,
        trade_count: int,
        bankroll: float,
    ) -> None:
        sign = "+" if pnl >= 0 else ""
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        text = (
            f"📈 <b>Daily Summary</b> {pnl_emoji}\n"
            f"Realized P&amp;L: {sign}${pnl:,.2f}\n"
            f"Win rate: {win_rate:.1%}\n"
            f"Trades: {trade_count}\n"
            f"Bankroll: ${bankroll:,.2f}"
        )
        await self._send(text)

    async def error_alert(self, component: str, error: str) -> None:
        text = (
            f"❌ <b>Error — {component}</b>\n"
            f"<code>{error[:500]}</code>"
        )
        await self._send(text)

    async def startup_notice(self, bankroll: float, whitelist_count: int) -> None:
        text = (
            f"🟢 <b>Bot Started</b>\n"
            f"Bankroll: ${bankroll:,.2f}\n"
            f"Whitelisted wallets: {whitelist_count}"
        )
        await self._send(text)

    async def shutdown_notice(self) -> None:
        text = "🔴 <b>Bot Shutting Down</b>\nGraceful shutdown initiated."
        await self._send(text)

    # ------------------------------------------------------------------
    # Simulation / paper-trading alerts
    # ------------------------------------------------------------------

    async def sim_startup_notice(self, virtual_bankroll: float, whitelist_count: int) -> None:
        text = (
            f"🟢 <b>Simulation Mode Started</b>\n"
            f"Virtual bankroll: ${virtual_bankroll:,.2f}\n"
            f"Whitelisted wallets: {whitelist_count}\n"
            f"<i>No real orders will be placed.</i>"
        )
        await self._send(text)

    async def sim_position_opened(
        self,
        market: str,
        size: float,
        fill_price: float,
        shares: float,
        whale_score: float,
        copy_of: str,
    ) -> None:
        short_wallet = f"{copy_of[:6]}...{copy_of[-4:]}" if len(copy_of) > 10 else copy_of
        text = (
            f"📋 <b>[SIM] Position Opened</b>\n"
            f"Market: {market}\n"
            f"Size: ${size:,.2f} @ ${fill_price:.4f}\n"
            f"Shares: {shares:.2f}\n"
            f"Copying: <code>{short_wallet}</code> (score: {whale_score:.1f})"
        )
        await self._send(text)

    async def sim_position_closed(
        self,
        market: str,
        pnl: float,
        exit_reason: str,
        entry_price: float,
        exit_price: float,
        size: float,
    ) -> None:
        pnl_emoji = "✅" if pnl >= 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        roi_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
        text = (
            f"📊 <b>[SIM] Position Closed</b> {pnl_emoji}\n"
            f"Market: {market}\n"
            f"Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f}\n"
            f"P&amp;L: {sign}${pnl:,.2f} ({sign}{roi_pct:.1f}%)\n"
            f"Reason: {exit_reason}"
        )
        await self._send(text)

    async def sim_performance_report(self, report_text: str) -> None:
        """Send a pre-formatted HTML performance report."""
        await self._send(report_text)

    async def sim_mark_to_market_update(
        self,
        open_positions: int,
        total_unrealized_pnl: float,
        virtual_bankroll: float,
    ) -> None:
        sign = "+" if total_unrealized_pnl >= 0 else ""
        text = (
            f"📈 <b>[SIM] Mark-to-Market</b>\n"
            f"Open positions: {open_positions}\n"
            f"Unrealized P&amp;L: {sign}${total_unrealized_pnl:,.2f}\n"
            f"Virtual bankroll: ${virtual_bankroll:,.2f}"
        )
        await self._send(text)
