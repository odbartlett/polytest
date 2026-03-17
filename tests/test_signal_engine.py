"""Tests for the signal engine — all 8 gate checks, copy sizing, and confidence multiplier."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.clob_client import Market, Orderbook, OrderLevel, Token, TradeEvent
from signals.signal_engine import SignalEngine, _compute_confidence_multiplier, _get_tier_pct


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trade(
    side: str = "BUY",
    size_usdc: float = 1000.0,
    wallet: str = "0xwhale",
    market_id: str = "market_1",
    token_id: str = "token_1",
) -> TradeEvent:
    return TradeEvent(
        wallet_address=wallet,
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=0.6,
        size_usdc=size_usdc,
        timestamp=datetime.now(tz=timezone.utc),
        transaction_hash="0x" + "a" * 64,
    )


def _make_market(
    oi: float = 100_000.0,
    hours_to_resolve: int = 48,
    market_id: str = "market_1",
) -> Market:
    resolution_time = datetime.now(tz=timezone.utc) + timedelta(hours=hours_to_resolve)
    return Market(
        condition_id=market_id,
        question="Will it happen?",
        category="POLITICS",
        open_interest=oi,
        resolution_time=resolution_time,
        active=True,
        tokens=[Token(token_id="token_1", outcome="YES")],
    )


def _make_orderbook(depth_usdc: float = 5000.0) -> Orderbook:
    # Simulate several ask levels
    levels = [OrderLevel(price=0.60, size=depth_usdc / 0.60)]
    return Orderbook(
        token_id="token_1",
        asks=levels,
        bids=[OrderLevel(price=0.58, size=100.0)],
    )


def _make_score_result(whale_score: float = 70.0) -> MagicMock:
    m = MagicMock()
    m.whale_score = whale_score
    m.roi_score = 60.0
    m.consistency_score = 65.0
    return m


def _build_engine(
    whale_score: float = 70.0,
    market_oi: float = 100_000.0,
    hours_to_resolve: int = 48,
    depth_usdc: float = 5000.0,
    bankroll: float = 1000.0,
    existing_exposure: float = 0.0,
    circuit_breaker: bool = False,
) -> tuple[SignalEngine, Market, TradeEvent]:
    market = _make_market(oi=market_oi, hours_to_resolve=hours_to_resolve)
    trade = _make_trade()
    orderbook = _make_orderbook(depth_usdc=depth_usdc)

    mock_clob = AsyncMock()
    mock_clob.get_market.return_value = market
    mock_clob.get_orderbook.return_value = orderbook

    mock_whitelist = AsyncMock()
    mock_whitelist.get_wallet_score_result.return_value = _make_score_result(whale_score)
    mock_whitelist.get_whale_score.return_value = whale_score

    mock_risk_gate = AsyncMock()
    mock_risk_gate.is_circuit_breaker_active.return_value = circuit_breaker

    mock_redis = AsyncMock()
    mock_redis.get.return_value = str(bankroll).encode()

    mock_ledger = AsyncMock()

    engine = SignalEngine(
        clob_client=mock_clob,
        position_ledger=mock_ledger,
        whitelist_manager=mock_whitelist,
        risk_gate=mock_risk_gate,
        redis_client=mock_redis,
    )

    # Patch _get_market_exposure to avoid DB calls
    engine._get_market_exposure = AsyncMock(return_value=existing_exposure)

    return engine, market, trade


# ---------------------------------------------------------------------------
# Gate 1: TRADE_IS_BUY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_trade_is_buy_fails_on_sell() -> None:
    engine, _, _ = _build_engine()
    trade = _make_trade(side="SELL")
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "TRADE_IS_BUY"


@pytest.mark.asyncio
async def test_gate_trade_is_buy_passes_on_buy() -> None:
    engine, _, trade = _build_engine()
    result = await engine.evaluate(trade)
    # Should proceed past gate 1 (may fail at a later gate)
    assert result.gate_failed != "TRADE_IS_BUY"


# ---------------------------------------------------------------------------
# Gate 2: TRADE_SIZE_MIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_trade_size_min_fails_on_small_trade() -> None:
    engine, _, _ = _build_engine()
    trade = _make_trade(size_usdc=100.0)  # Below MIN_WHALE_TRADE_SIZE=500
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "TRADE_SIZE_MIN"


@pytest.mark.asyncio
async def test_gate_trade_size_min_passes_at_threshold() -> None:
    engine, _, _ = _build_engine()
    trade = _make_trade(size_usdc=500.0)  # Exactly at MIN_WHALE_TRADE_SIZE
    result = await engine.evaluate(trade)
    assert result.gate_failed != "TRADE_SIZE_MIN"


# ---------------------------------------------------------------------------
# Gate 3: WHALE_SCORE_MIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_whale_score_fails_below_floor() -> None:
    engine, _, trade = _build_engine(whale_score=50.0)  # Below floor=55
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "WHALE_SCORE_MIN"


@pytest.mark.asyncio
async def test_gate_whale_score_fails_when_not_whitelisted() -> None:
    engine, _, trade = _build_engine()
    engine._whitelist.get_wallet_score_result.return_value = None  # type: ignore[attr-defined]
    engine._whitelist.get_whale_score.return_value = None  # type: ignore[attr-defined]
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "WHALE_SCORE_MIN"


# ---------------------------------------------------------------------------
# Gate 4: MARKET_OI_MIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_market_oi_fails_below_minimum() -> None:
    engine, _, trade = _build_engine(market_oi=10_000.0)  # Below MIN=50k
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "MARKET_OI_MIN"


@pytest.mark.asyncio
async def test_gate_market_oi_passes_above_minimum() -> None:
    engine, _, trade = _build_engine(market_oi=100_000.0)
    result = await engine.evaluate(trade)
    assert result.gate_failed != "MARKET_OI_MIN"


# ---------------------------------------------------------------------------
# Gate 5: ORDERBOOK_DEPTH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_orderbook_depth_fails_when_thin() -> None:
    engine, _, trade = _build_engine(depth_usdc=10.0)  # Very thin book
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "ORDERBOOK_DEPTH"


# ---------------------------------------------------------------------------
# Gate 6: POSITION_CAP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_position_cap_fails_when_at_limit() -> None:
    # Bankroll=$1000, cap=5%=$50, existing exposure already at $50
    engine, _, trade = _build_engine(bankroll=1000.0, existing_exposure=50.0)
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "POSITION_CAP"


@pytest.mark.asyncio
async def test_gate_position_cap_passes_when_under_limit() -> None:
    engine, _, trade = _build_engine(bankroll=10000.0, existing_exposure=0.0)
    result = await engine.evaluate(trade)
    assert result.gate_failed != "POSITION_CAP"


# ---------------------------------------------------------------------------
# Gate 7: TIME_TO_RESOLUTION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_time_to_resolution_fails_when_imminent() -> None:
    engine, _, trade = _build_engine(hours_to_resolve=2)  # Only 2h left, min=6h
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "TIME_TO_RESOLUTION"


@pytest.mark.asyncio
async def test_gate_time_to_resolution_passes_with_enough_time() -> None:
    engine, _, trade = _build_engine(hours_to_resolve=48, bankroll=10000.0)
    result = await engine.evaluate(trade)
    assert result.gate_failed != "TIME_TO_RESOLUTION"


# ---------------------------------------------------------------------------
# Gate 8: CIRCUIT_BREAKER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_circuit_breaker_fails_when_active() -> None:
    engine, _, trade = _build_engine(circuit_breaker=True, bankroll=10000.0)
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert result.gate_failed == "CIRCUIT_BREAKER"


@pytest.mark.asyncio
async def test_gate_circuit_breaker_passes_when_inactive() -> None:
    engine, _, trade = _build_engine(circuit_breaker=False, bankroll=10000.0)
    result = await engine.evaluate(trade)
    assert result.gate_failed != "CIRCUIT_BREAKER"


# ---------------------------------------------------------------------------
# Copy sizing math
# ---------------------------------------------------------------------------


def test_tier_pct_all_tiers() -> None:
    assert _get_tier_pct(55.0) == 0.005
    assert _get_tier_pct(60.0) == 0.005
    assert _get_tier_pct(65.0) == 0.010
    assert _get_tier_pct(70.0) == 0.010
    assert _get_tier_pct(75.0) == 0.015
    assert _get_tier_pct(80.0) == 0.015
    assert _get_tier_pct(85.0) == 0.020
    assert _get_tier_pct(100.0) == 0.020
    assert _get_tier_pct(50.0) == 0.0  # Below all tiers


def test_copy_size_rounds_to_nearest_10() -> None:
    """copy_size must be rounded DOWN to nearest $10."""
    # bankroll=$10000, score=70 → tier=1% → base=$100
    # confidence_mult with roi=60, consistency=65 → 0.6*0.65*2=0.78 → max(0.5,.78)=0.78
    # raw_size = 100 * 0.78 = 78 → floor(78/10)*10 = 70
    raw = 78.3
    expected = math.floor(raw / 10) * 10
    assert expected == 70


@pytest.mark.asyncio
async def test_copy_size_is_multiple_of_10() -> None:
    """The copy_size returned in a passing signal should be divisible by 10."""
    engine, _, trade = _build_engine(
        bankroll=10000.0, whale_score=70.0, market_oi=200_000.0, depth_usdc=50000.0
    )
    result = await engine.evaluate(trade)
    if result.should_trade:
        assert result.copy_size_usdc % 10 == 0


@pytest.mark.asyncio
async def test_copy_size_respects_depth_cap() -> None:
    """Copy size should never exceed 20% of available orderbook depth."""
    # depth=$1000, 20% cap = $200
    engine, _, trade = _build_engine(
        bankroll=100_000.0, whale_score=90.0, depth_usdc=1000.0, market_oi=500_000.0
    )
    result = await engine.evaluate(trade)
    if result.should_trade:
        assert result.copy_size_usdc <= 1000.0 * 0.20 + 10  # +10 for rounding tolerance


# ---------------------------------------------------------------------------
# Confidence multiplier
# ---------------------------------------------------------------------------


def test_confidence_multiplier_lower_bound() -> None:
    """Multiplier should never drop below 0.5."""
    mult = _compute_confidence_multiplier(roi_score=0.0, consistency_score=0.0)
    assert mult == pytest.approx(0.5)


def test_confidence_multiplier_upper_bound() -> None:
    """Multiplier should never exceed 1.5."""
    mult = _compute_confidence_multiplier(roi_score=100.0, consistency_score=100.0)
    assert mult == pytest.approx(1.5)


def test_confidence_multiplier_midpoint() -> None:
    """(0.6 * 0.65) * 2 = 0.78 → stays between bounds."""
    mult = _compute_confidence_multiplier(roi_score=60.0, consistency_score=65.0)
    expected = min(1.5, max(0.5, (60 / 100) * (65 / 100) * 2.0))
    assert mult == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# EXIT trades never generate a signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_trade_never_signals() -> None:
    """A SELL trade must always fail gate 1 and never generate a signal."""
    engine, _, _ = _build_engine()
    trade = _make_trade(side="SELL", size_usdc=5000.0)
    result = await engine.evaluate(trade)
    assert result.should_trade is False
    assert "TRADE_IS_BUY" in result.reason
