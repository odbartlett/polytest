"""Tests for the risk gate — circuit breaker, exposure cap, bankroll updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from data.clob_client import Market, Token
from execution.risk_gate import (
    BANKROLL_KEY,
    CIRCUIT_BREAKER_KEY,
    PEAK_BANKROLL_KEY,
    RiskGate,
)
from signals.signal_engine import SignalDecision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_redis(bankroll: float = 1000.0, peak: float = 1000.0) -> AsyncMock:
    """In-memory mock Redis for risk gate tests."""
    store: dict[str, str] = {
        BANKROLL_KEY: str(bankroll),
        PEAK_BANKROLL_KEY: str(peak),
    }

    redis = AsyncMock()

    async def get(key: str) -> bytes | None:
        v = store.get(key)
        return v.encode() if v is not None else None

    async def set_val(key: str, value: str) -> None:
        store[key] = value

    async def delete(key: str) -> None:
        store.pop(key, None)

    redis.get = AsyncMock(side_effect=get)
    redis.set = AsyncMock(side_effect=set_val)
    redis.delete = AsyncMock(side_effect=delete)

    # Expose store for assertions
    redis._store = store
    return redis


def _make_signal(copy_size: float = 40.0, whale_score: float = 70.0) -> SignalDecision:
    return SignalDecision(
        should_trade=True,
        copy_size_usdc=copy_size,
        reason="All gates passed",
        whale_score=whale_score,
        roi_score=60.0,
        consistency_score=65.0,
    )


def _make_market(market_id: str = "market_1") -> Market:
    from datetime import datetime, timedelta, timezone
    return Market(
        condition_id=market_id,
        question="Will it happen?",
        category="POLITICS",
        open_interest=100_000.0,
        resolution_time=datetime.now(tz=timezone.utc) + timedelta(hours=48),
        active=True,
        tokens=[Token(token_id="token_1", outcome="YES")],
    )


async def _make_gate(
    bankroll: float = 1000.0,
    peak: float = 1000.0,
    circuit_breaker_active: bool = False,
) -> RiskGate:
    redis = _make_redis(bankroll=bankroll, peak=peak)
    if circuit_breaker_active:
        redis._store[CIRCUIT_BREAKER_KEY] = "1"

    mock_alerter = AsyncMock()
    gate = RiskGate(redis_client=redis, alerter=mock_alerter)
    gate._initial_bankroll = bankroll
    return gate


# ---------------------------------------------------------------------------
# Circuit breaker activation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_activates_at_max_drawdown() -> None:
    """Circuit breaker should trigger when drawdown equals MAX_DRAWDOWN_PCT."""
    from config.settings import get_settings
    settings = get_settings()
    peak = 1000.0
    # At exactly MAX_DRAWDOWN_PCT (15%), bankroll = peak * (1 - 0.15) = 850
    current = peak * (1 - settings.MAX_DRAWDOWN_PCT)

    gate = await _make_gate(bankroll=current, peak=peak)
    signal = _make_signal(copy_size=40.0)
    result = await gate.check(signal, _make_market())

    assert result.passed is False
    assert "drawdown" in result.reason.lower() or "circuit" in result.reason.lower()


@pytest.mark.asyncio
async def test_circuit_breaker_not_triggered_below_threshold() -> None:
    """Circuit breaker should NOT trigger when drawdown is below MAX_DRAWDOWN_PCT."""
    gate = await _make_gate(bankroll=900.0, peak=1000.0)  # 10% drawdown (< 15%)
    signal = _make_signal(copy_size=40.0)
    result = await gate.check(signal, _make_market())
    # Should pass (unless another gate fails)
    assert "drawdown" not in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_circuit_breaker_already_active_blocks_trades() -> None:
    """When circuit breaker flag is already set, all trades must be blocked."""
    gate = await _make_gate(bankroll=900.0, peak=1000.0, circuit_breaker_active=True)
    signal = _make_signal(copy_size=40.0)
    result = await gate.check(signal, _make_market())
    assert result.passed is False
    assert "circuit breaker" in result.reason.lower()


@pytest.mark.asyncio
async def test_circuit_breaker_sends_telegram_alert() -> None:
    """Triggering the circuit breaker should invoke the alerter."""
    from config.settings import get_settings
    settings = get_settings()
    peak = 1000.0
    current = peak * (1 - settings.MAX_DRAWDOWN_PCT)

    redis = _make_redis(bankroll=current, peak=peak)
    mock_alerter = AsyncMock()
    gate = RiskGate(redis_client=redis, alerter=mock_alerter)
    gate._initial_bankroll = peak

    signal = _make_signal(copy_size=40.0)
    await gate.check(signal, _make_market())

    # Alerter should have been called
    mock_alerter.circuit_breaker_triggered.assert_called_once()


# ---------------------------------------------------------------------------
# Per-market exposure cap tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exposure_cap_blocks_oversized_trade() -> None:
    """Copy size exceeding MAX_PER_MARKET_EXPOSURE_PCT * bankroll must be blocked."""
    from config.settings import get_settings
    settings = get_settings()
    bankroll = 1000.0
    # Max per-market = 5% * 1000 = $50
    # Signal copy size = $60 > $50 → should fail
    gate = await _make_gate(bankroll=bankroll, peak=bankroll)
    signal = _make_signal(copy_size=60.0)
    result = await gate.check(signal, _make_market())
    assert result.passed is False
    assert "cap" in result.reason.lower() or "exposure" in result.reason.lower()


@pytest.mark.asyncio
async def test_exposure_cap_allows_at_limit() -> None:
    """Copy size exactly at the cap should be allowed."""
    bankroll = 1000.0
    gate = await _make_gate(bankroll=bankroll, peak=bankroll)
    # Max = 5% * 1000 = $50, signal is exactly $50
    signal = _make_signal(copy_size=50.0)
    result = await gate.check(signal, _make_market())
    assert result.passed is True


# ---------------------------------------------------------------------------
# Bankroll update tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bankroll_update_persists_to_redis() -> None:
    """update_bankroll should write the new value to Redis."""
    gate = await _make_gate(bankroll=1000.0, peak=1000.0)
    await gate.update_bankroll(1200.0)
    new_bankroll = await gate.get_bankroll()
    assert new_bankroll == pytest.approx(1200.0)


@pytest.mark.asyncio
async def test_bankroll_update_raises_peak_when_higher() -> None:
    """update_bankroll should update peak when new value exceeds current peak."""
    gate = await _make_gate(bankroll=1000.0, peak=1000.0)
    await gate.update_bankroll(1500.0)

    # Re-read peak
    peak_raw = await gate._redis.get(PEAK_BANKROLL_KEY)
    assert float(peak_raw) == pytest.approx(1500.0)


@pytest.mark.asyncio
async def test_bankroll_update_does_not_lower_peak() -> None:
    """update_bankroll should NOT update peak when new value is lower."""
    gate = await _make_gate(bankroll=1000.0, peak=2000.0)
    await gate.update_bankroll(800.0)  # Drawdown

    peak_raw = await gate._redis.get(PEAK_BANKROLL_KEY)
    assert float(peak_raw) == pytest.approx(2000.0)


@pytest.mark.asyncio
async def test_drawdown_propagates_to_check() -> None:
    """After a bankroll drop, get_current_drawdown should reflect the new drawdown."""
    gate = await _make_gate(bankroll=1000.0, peak=1000.0)
    await gate.update_bankroll(850.0)

    drawdown = await gate.get_current_drawdown()
    # (1000 - 850) / 1000 = 0.15
    assert drawdown == pytest.approx(0.15, abs=1e-6)


# ---------------------------------------------------------------------------
# get_current_drawdown edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drawdown_is_zero_at_peak() -> None:
    gate = await _make_gate(bankroll=1000.0, peak=1000.0)
    drawdown = await gate.get_current_drawdown()
    assert drawdown == pytest.approx(0.0, abs=1e-9)


@pytest.mark.asyncio
async def test_drawdown_is_non_negative() -> None:
    """Drawdown must never be negative (no gain is reported as drawdown)."""
    gate = await _make_gate(bankroll=1200.0, peak=1000.0)  # 20% gain
    drawdown = await gate.get_current_drawdown()
    assert drawdown == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Circuit breaker reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_reset_allows_trading() -> None:
    """After reset_circuit_breaker(), is_circuit_breaker_active() should return False."""
    gate = await _make_gate(circuit_breaker_active=True)
    assert await gate.is_circuit_breaker_active() is True

    await gate.reset_circuit_breaker()
    assert await gate.is_circuit_breaker_active() is False


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_sets_bankroll_when_missing() -> None:
    """On first run (no Redis key), initialize() should set the initial bankroll."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)  # Key doesn't exist yet
    redis.set = AsyncMock()

    from config.settings import get_settings
    settings = get_settings()

    gate = RiskGate(redis_client=redis)
    await gate.initialize()

    # Should have set the bankroll to BANKROLL_USDC
    redis.set.assert_called()
    calls = [str(c) for c in redis.set.call_args_list]
    assert any(str(settings.BANKROLL_USDC) in c for c in calls)
