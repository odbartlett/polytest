"""Tests for the paper trader — VWAP fill simulation, position persistence, bankroll."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.clob_client import Market, Orderbook, OrderLevel, Token
from simulation.paper_trader import PaperTrader, _compute_vwap_fill, _score_tier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_orderbook(ask_levels: list[tuple[float, float]]) -> Orderbook:
    """Build an Orderbook with given (price, size) ask levels."""
    asks = [OrderLevel(price=p, size=s) for p, s in ask_levels]
    bids = [OrderLevel(price=ask_levels[0][0] - 0.01, size=100.0)] if ask_levels else []
    token_id = "token_abc"
    return Orderbook(token_id=token_id, asks=asks, bids=bids)


def _make_market(market_id: str = "mkt_1", question: str = "Will it happen?") -> Market:
    return Market(
        condition_id=market_id,
        question=question,
        category="POLITICS",
        open_interest=100_000.0,
        resolution_time=datetime(2025, 12, 31, tzinfo=timezone.utc),
        active=True,
        tokens=[Token(token_id="token_abc", outcome="YES")],
    )


def _make_signal(
    should_trade: bool = True,
    copy_size_usdc: float = 100.0,
    whale_score: float = 70.0,
    roi_score: float = 60.0,
    consistency_score: float = 65.0,
) -> MagicMock:
    s = MagicMock()
    s.should_trade = should_trade
    s.copy_size_usdc = copy_size_usdc
    s.whale_score = whale_score
    s.roi_score = roi_score
    s.consistency_score = consistency_score
    s.gate_failed = None
    s.reason = ""
    return s


def _make_paper_trader(
    orderbook: Orderbook | None = None,
    sim_bankroll: float = 10_000.0,
) -> PaperTrader:
    mock_clob = AsyncMock()
    mock_alerter = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=str(sim_bankroll).encode())
    mock_redis.set = AsyncMock()

    if orderbook is not None:
        mock_clob.get_orderbook.return_value = orderbook

    return PaperTrader(
        clob_client=mock_clob,
        alerter=mock_alerter,
        redis_client=mock_redis,
    )


# ---------------------------------------------------------------------------
# _compute_vwap_fill tests
# ---------------------------------------------------------------------------


def test_vwap_fill_single_level_exact_fit() -> None:
    """If all size fits in first ask level, VWAP equals that ask price + slippage."""
    book = _make_orderbook([(0.60, 1000.0)])  # capacity = 600 USDC
    # We want to buy $100 at price $0.60 → 166.67 shares
    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_FILL_SLIPPAGE = 0.001
        fill = _compute_vwap_fill(book, 100.0)
    # Expected: 0.60 * (1 + 0.001) = 0.6006
    assert fill == pytest.approx(0.60 * 1.001, rel=1e-6)


def test_vwap_fill_across_multiple_levels() -> None:
    """VWAP is correctly computed when size spans multiple ask levels."""
    # Level 1: price=0.50, size=100 → capacity = 50 USDC
    # Level 2: price=0.60, size=100 → capacity = 60 USDC
    # Total size = 110 USDC → 100 shares at L1 + 16.67 shares at L2
    book = _make_orderbook([(0.50, 100.0), (0.60, 100.0)])
    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_FILL_SLIPPAGE = 0.0  # no slippage for clarity
        fill = _compute_vwap_fill(book, 110.0)

    # 100 shares @ 0.50 + 16.667 shares @ 0.60 = 116.667 shares, cost = 110
    # vwap = 110 / 116.667 ≈ 0.9429... wait, let me recalculate
    # L1: capacity = 0.50 * 100 = 50 USDC, shares = 50/0.50 = 100 shares
    # L2: remaining = 60 USDC, capacity = 60 USDC, shares = 60/0.60 = 100 shares
    # vwap = 110 / 200 = 0.55
    assert fill == pytest.approx(0.55, rel=1e-4)


def test_vwap_fill_empty_orderbook_uses_fallback() -> None:
    """Empty orderbook falls back to 0.5 mid-price + slippage."""
    book = Orderbook(token_id="tok", asks=[], bids=[])
    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_FILL_SLIPPAGE = 0.001
        fill = _compute_vwap_fill(book, 100.0)
    assert fill == pytest.approx(0.5 * 1.001, rel=1e-6)


def test_vwap_fill_slippage_applied() -> None:
    """SIM_FILL_SLIPPAGE is multiplicatively added to the raw VWAP."""
    book = _make_orderbook([(0.60, 10_000.0)])
    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_FILL_SLIPPAGE = 0.005  # 0.5%
        fill = _compute_vwap_fill(book, 500.0)
    assert fill == pytest.approx(0.60 * 1.005, rel=1e-6)


# ---------------------------------------------------------------------------
# _score_tier tests
# ---------------------------------------------------------------------------


def test_score_tier_boundaries() -> None:
    assert _score_tier(55.0) == "55-65"
    assert _score_tier(64.9) == "55-65"
    assert _score_tier(65.0) == "65-75"
    assert _score_tier(74.9) == "65-75"
    assert _score_tier(75.0) == "75-85"
    assert _score_tier(84.9) == "75-85"
    assert _score_tier(85.0) == "85+"
    assert _score_tier(100.0) == "85+"
    assert _score_tier(40.0) == "unknown"


# ---------------------------------------------------------------------------
# PaperTrader.execute() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_returns_success() -> None:
    """execute() should succeed when orderbook data is available."""
    book = _make_orderbook([(0.65, 5000.0)])
    trader = _make_paper_trader(orderbook=book)
    signal = _make_signal(copy_size_usdc=100.0, whale_score=70.0)
    market = _make_market()

    with (
        patch("simulation.paper_trader.AsyncSessionLocal") as mock_session_cls,
        patch("simulation.paper_trader.get_settings") as mock_settings,
    ):
        mock_settings.return_value.SIM_FILL_SLIPPAGE = 0.001
        # Mock the session context manager
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = False
        mock_session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
        mock_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.flush = AsyncMock()

        position_mock = MagicMock()
        position_mock.id = 42

        def _add(obj):
            if hasattr(obj, "id"):
                obj.id = 42

        mock_session.add = MagicMock(side_effect=_add)
        mock_session_cls.return_value = mock_session

        result = await trader.execute(signal=signal, market=market, copied_from_wallet="0xwhale")

    assert result.success is True
    assert result.order_id is not None


@pytest.mark.asyncio
async def test_execute_no_tokens_returns_failure() -> None:
    """Markets with no token list should return a failed result immediately."""
    book = _make_orderbook([(0.60, 1000.0)])
    trader = _make_paper_trader(orderbook=book)
    signal = _make_signal(copy_size_usdc=100.0)

    market_no_tokens = Market(
        condition_id="mkt_empty",
        question="Empty market?",
        active=True,
        tokens=[],
    )

    result = await trader.execute(signal=signal, market=market_no_tokens, copied_from_wallet="0xwhale")
    assert result.success is False
    assert "token" in result.reason.lower()


@pytest.mark.asyncio
async def test_execute_orderbook_failure_uses_fallback_price() -> None:
    """When the orderbook fetch fails, a fallback price (0.5 + slippage) is used."""
    trader = _make_paper_trader()
    trader._clob.get_orderbook.side_effect = Exception("Network error")
    signal = _make_signal(copy_size_usdc=100.0)
    market = _make_market()

    with (
        patch("simulation.paper_trader.AsyncSessionLocal") as mock_session_cls,
        patch("simulation.paper_trader.get_settings") as mock_settings,
    ):
        mock_settings.return_value.SIM_FILL_SLIPPAGE = 0.001
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = False
        mock_session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
        mock_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session.flush = AsyncMock()

        position_mock = MagicMock()
        position_mock.id = 99
        mock_session.add = MagicMock()
        mock_session_cls.return_value = mock_session

        result = await trader.execute(signal=signal, market=market, copied_from_wallet="0xwhale")

    # Should succeed with fallback price, not crash
    assert result.success is True


# ---------------------------------------------------------------------------
# Bankroll helper tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sim_bankroll_from_redis() -> None:
    """get_sim_bankroll() reads the cached value from Redis."""
    mock_clob = AsyncMock()
    mock_alerter = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"12500.0")
    mock_redis.set = AsyncMock()

    trader = PaperTrader(clob_client=mock_clob, alerter=mock_alerter, redis_client=mock_redis)

    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_BANKROLL_USDC = 10_000.0
        bankroll = await trader.get_sim_bankroll()

    assert bankroll == pytest.approx(12500.0)


@pytest.mark.asyncio
async def test_get_sim_bankroll_falls_back_to_config() -> None:
    """When Redis returns None, falls back to SIM_BANKROLL_USDC setting."""
    mock_clob = AsyncMock()
    mock_alerter = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    trader = PaperTrader(clob_client=mock_clob, alerter=mock_alerter, redis_client=mock_redis)

    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_BANKROLL_USDC = 9999.0
        bankroll = await trader.get_sim_bankroll()

    assert bankroll == pytest.approx(9999.0)


@pytest.mark.asyncio
async def test_initialize_bankroll_sets_keys_when_missing() -> None:
    """initialize_bankroll() should write to Redis when no value exists."""
    mock_clob = AsyncMock()
    mock_alerter = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    trader = PaperTrader(clob_client=mock_clob, alerter=mock_alerter, redis_client=mock_redis)

    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_BANKROLL_USDC = 10_000.0
        await trader.initialize_bankroll()

    assert mock_redis.set.call_count == 2  # SIM_BANKROLL_KEY + SIM_PEAK_BANKROLL_KEY


@pytest.mark.asyncio
async def test_initialize_bankroll_skips_when_already_set() -> None:
    """initialize_bankroll() must not overwrite an existing bankroll."""
    mock_clob = AsyncMock()
    mock_alerter = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"10000.0")  # already set
    mock_redis.set = AsyncMock()

    trader = PaperTrader(clob_client=mock_clob, alerter=mock_alerter, redis_client=mock_redis)

    with patch("simulation.paper_trader.get_settings") as mock_settings:
        mock_settings.return_value.SIM_BANKROLL_USDC = 10_000.0
        await trader.initialize_bankroll()

    mock_redis.set.assert_not_called()
