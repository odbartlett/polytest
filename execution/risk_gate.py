"""Pre-trade risk checks and circuit breaker management.

Bankroll and peak bankroll are tracked in Redis:
  bot:bankroll       — current bankroll (USDC float)
  bot:peak_bankroll  — all-time peak bankroll (USDC float)
  bot:circuit_breaker_active — "1" when active, must be manually reset

The circuit breaker trips when:
  current_bankroll < initial_bankroll * (1 - MAX_DRAWDOWN_PCT)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from pydantic import BaseModel

from config.settings import get_settings
from data.clob_client import Market
from signals.signal_engine import SignalDecision

logger = structlog.get_logger(__name__)

_settings = get_settings()

BANKROLL_KEY = "bot:bankroll"
PEAK_BANKROLL_KEY = "bot:peak_bankroll"
CIRCUIT_BREAKER_KEY = "bot:circuit_breaker_active"


class RiskCheckResult(BaseModel):
    passed: bool
    reason: str
    current_drawdown_pct: float = 0.0
    current_bankroll: float = 0.0


class RiskGate:
    """Enforces pre-trade risk limits and manages the circuit breaker."""

    def __init__(self, redis_client: object, alerter: Optional[object] = None) -> None:
        self._redis = redis_client
        self._alerter = alerter  # TelegramAlerter (injected post-init to break circular dep)
        self._settings = get_settings()
        # Cache the initial bankroll for drawdown reference
        self._initial_bankroll: Optional[float] = None

    async def initialize(self) -> None:
        """Bootstrap Redis keys on first run."""
        try:
            existing = await self._redis.get(BANKROLL_KEY)  # type: ignore[union-attr]
            if existing is None:
                initial = self._settings.BANKROLL_USDC
                await self._redis.set(BANKROLL_KEY, str(initial))  # type: ignore[union-attr]
                await self._redis.set(PEAK_BANKROLL_KEY, str(initial))  # type: ignore[union-attr]
                logger.info("risk_gate.initialized", initial_bankroll=initial)
            self._initial_bankroll = float(existing or self._settings.BANKROLL_USDC)
        except Exception as exc:
            logger.error("risk_gate.init_failed", error=str(exc))
            self._initial_bankroll = self._settings.BANKROLL_USDC

    async def check(self, signal: SignalDecision, market: Market) -> RiskCheckResult:
        """Run final risk checks before order placement.

        This is called immediately before submitting an order, after signal
        evaluation has already passed all 8 gates. It re-validates the most
        critical constraints at the point of execution.
        """
        current_bankroll = await self.get_bankroll()
        drawdown_pct = await self.get_current_drawdown()

        # Re-check circuit breaker
        if await self.is_circuit_breaker_active():
            return RiskCheckResult(
                passed=False,
                reason="Circuit breaker is active — trading halted",
                current_drawdown_pct=drawdown_pct,
                current_bankroll=current_bankroll,
            )

        # Re-check drawdown (may have changed since signal evaluation)
        if drawdown_pct >= self._settings.MAX_DRAWDOWN_PCT:
            await self._trigger_circuit_breaker(current_bankroll, drawdown_pct)
            return RiskCheckResult(
                passed=False,
                reason=f"Drawdown {drawdown_pct:.1%} >= limit {self._settings.MAX_DRAWDOWN_PCT:.1%}",
                current_drawdown_pct=drawdown_pct,
                current_bankroll=current_bankroll,
            )

        # Re-check per-market exposure cap
        max_exposure = current_bankroll * self._settings.MAX_PER_MARKET_EXPOSURE_PCT
        if signal.copy_size_usdc > max_exposure:
            return RiskCheckResult(
                passed=False,
                reason=(
                    f"Copy size ${signal.copy_size_usdc:.2f} exceeds "
                    f"market cap ${max_exposure:.2f}"
                ),
                current_drawdown_pct=drawdown_pct,
                current_bankroll=current_bankroll,
            )

        # Verify sufficient free capital
        if signal.copy_size_usdc > current_bankroll * 0.95:
            return RiskCheckResult(
                passed=False,
                reason="Insufficient free capital",
                current_drawdown_pct=drawdown_pct,
                current_bankroll=current_bankroll,
            )

        return RiskCheckResult(
            passed=True,
            reason="All risk checks passed",
            current_drawdown_pct=drawdown_pct,
            current_bankroll=current_bankroll,
        )

    async def get_current_drawdown(self) -> float:
        """Return current drawdown as a fraction (0.0 – 1.0)."""
        try:
            peak_raw = await self._redis.get(PEAK_BANKROLL_KEY)  # type: ignore[union-attr]
            current_raw = await self._redis.get(BANKROLL_KEY)  # type: ignore[union-attr]
            if peak_raw is None or current_raw is None:
                return 0.0
            peak = float(peak_raw)
            current = float(current_raw)
            if peak <= 0:
                return 0.0
            return max(0.0, (peak - current) / peak)
        except Exception as exc:
            logger.warning("risk_gate.drawdown_read_failed", error=str(exc))
            return 0.0

    async def get_bankroll(self) -> float:
        """Return the current bankroll from Redis."""
        try:
            val = await self._redis.get(BANKROLL_KEY)  # type: ignore[union-attr]
            return float(val) if val is not None else self._settings.BANKROLL_USDC
        except Exception as exc:
            logger.warning("risk_gate.bankroll_read_failed", error=str(exc))
            return self._settings.BANKROLL_USDC

    async def update_bankroll(self, new_value: float) -> None:
        """Update the bankroll in Redis and update peak if higher."""
        try:
            await self._redis.set(BANKROLL_KEY, str(new_value))  # type: ignore[union-attr]
            peak_raw = await self._redis.get(PEAK_BANKROLL_KEY)  # type: ignore[union-attr]
            peak = float(peak_raw) if peak_raw is not None else 0.0
            if new_value > peak:
                await self._redis.set(PEAK_BANKROLL_KEY, str(new_value))  # type: ignore[union-attr]
                logger.info("risk_gate.peak_updated", peak=new_value)
            logger.debug("risk_gate.bankroll_updated", bankroll=new_value)
        except Exception as exc:
            logger.error("risk_gate.bankroll_update_failed", error=str(exc))

    async def is_circuit_breaker_active(self) -> bool:
        """Return True if the circuit breaker flag is set in Redis."""
        try:
            val = await self._redis.get(CIRCUIT_BREAKER_KEY)  # type: ignore[union-attr]
            return val is not None and val not in (b"0", "0", b"", "")
        except Exception as exc:
            logger.warning("risk_gate.circuit_breaker_check_failed", error=str(exc))
            return False  # Fail open — don't block trading on Redis error

    async def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker (operator action required)."""
        try:
            await self._redis.delete(CIRCUIT_BREAKER_KEY)  # type: ignore[union-attr]
            logger.warning("risk_gate.circuit_breaker_reset")
        except Exception as exc:
            logger.error("risk_gate.circuit_breaker_reset_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _trigger_circuit_breaker(self, current_bankroll: float, drawdown_pct: float) -> None:
        """Activate the circuit breaker and send a CRITICAL Telegram alert."""
        try:
            await self._redis.set(CIRCUIT_BREAKER_KEY, "1")  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("risk_gate.circuit_breaker_set_failed", error=str(exc))

        logger.critical(
            "risk_gate.circuit_breaker_triggered",
            current_bankroll=current_bankroll,
            drawdown_pct=drawdown_pct,
        )

        if self._alerter is not None:
            try:
                await self._alerter.circuit_breaker_triggered(  # type: ignore[attr-defined]
                    current_bankroll=current_bankroll,
                    drawdown_pct=drawdown_pct,
                )
            except Exception as exc:
                logger.error("risk_gate.alert_failed", error=str(exc))
