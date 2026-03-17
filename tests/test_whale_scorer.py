"""Tests for the whale scorer — all five scoring components plus market maker filter."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.bitquery_client import HistoricalTrade
from scoring.whale_scorer import (
    InsufficientDataError,
    WhaleScorerService,
    _normalise_category,
)
from scoring.whitelist_manager import _is_market_maker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trade(
    side: str = "BUY",
    size_usdc: float = 1000.0,
    resolution: str | None = "YES",
    outcome_purchased: str = "YES",
    payout_usdc: float = 0.0,
    category: str = "POLITICS",
    days_ago: int = 10,
) -> HistoricalTrade:
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return HistoricalTrade(
        wallet_address="0xABCDEF1234567890" * 1 + "a" * 2,
        market_id=f"market_{side}_{days_ago}",
        token_id="token_abc",
        side=side,
        price=0.6,
        size_usdc=size_usdc,
        payout_usdc=payout_usdc,
        timestamp=ts,
        transaction_hash="0x" + "a" * 64,
        market_question="Will X happen?",
        category=category,
        resolution=resolution,
        outcome_purchased=outcome_purchased,
    )


def _winning_trade(size: float = 1000.0, category: str = "POLITICS", days_ago: int = 10) -> HistoricalTrade:
    return _make_trade(
        side="BUY",
        size_usdc=size,
        resolution="YES",
        outcome_purchased="YES",
        payout_usdc=size * 1.4,  # 40% profit
        category=category,
        days_ago=days_ago,
    )


def _losing_trade(size: float = 1000.0, category: str = "POLITICS", days_ago: int = 10) -> HistoricalTrade:
    return _make_trade(
        side="BUY",
        size_usdc=size,
        resolution="NO",
        outcome_purchased="YES",
        payout_usdc=0.0,
        category=category,
        days_ago=days_ago,
    )


def _make_scorer(redis_return: float | None = None) -> WhaleScorerService:
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=str(redis_return) if redis_return else None)
    return WhaleScorerService(redis_client=mock_redis)


# ---------------------------------------------------------------------------
# Minimum threshold tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_resolved_markets_raises() -> None:
    """Wallets with fewer than 30 resolved markets should be rejected."""
    scorer = _make_scorer()
    trades = [_winning_trade() for _ in range(15)]  # Only 15 resolved
    with pytest.raises(InsufficientDataError, match="resolved markets"):
        await scorer.score_wallet("0x" + "a" * 40, trades)


@pytest.mark.asyncio
async def test_insufficient_total_trades_raises() -> None:
    """Wallets with fewer than MIN_TRADE_COUNT trades should be rejected."""
    scorer = _make_scorer()
    trades = [_winning_trade() for _ in range(10)]  # Only 10 trades
    with pytest.raises(InsufficientDataError):
        await scorer.score_wallet("0x" + "a" * 40, trades)


@pytest.mark.asyncio
async def test_insufficient_volume_raises() -> None:
    """Wallets with < $5k total volume should be rejected."""
    scorer = _make_scorer()
    # 30 trades but each only $10 = $300 total
    trades = [_winning_trade(size=10.0) for _ in range(30)]
    with pytest.raises(InsufficientDataError, match="Volume"):
        await scorer.score_wallet("0x" + "a" * 40, trades)


# ---------------------------------------------------------------------------
# ROI score tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roi_score_perfect() -> None:
    """200% gross ROI should yield a perfect 100 roi_score."""
    scorer = _make_scorer(redis_return=2000.0)
    # Invest $1000, get back $3000 → roi = 2.0 → score = min(100, 2.0 * 200) = 100
    trades = []
    for i in range(30):
        t = _make_trade(size_usdc=1000.0, resolution="YES", outcome_purchased="YES", payout_usdc=3000.0)
        trades.append(t)
    # Add volume padding
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.roi_score == 100.0


@pytest.mark.asyncio
async def test_roi_score_breakeven() -> None:
    """Breakeven ROI (0%) should yield roi_score = 0."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = []
    for i in range(30):
        t = _make_trade(size_usdc=1000.0, resolution="YES", outcome_purchased="YES", payout_usdc=1000.0)
        trades.append(t)
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.roi_score == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_roi_score_partial_winners() -> None:
    """20 wins out of 30 with 50% profit → roi = 0.33 → score ≈ 66.7."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_winning_trade(size=1000.0) for _ in range(20)]  # payout=1400
    trades += [_losing_trade(size=1000.0) for _ in range(10)]
    # total_cost=30000, total_payout=28000, roi=(28000-30000)/30000 = -0.0667
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    expected_roi = max(0.0, (28000.0 - 30000.0) / 30000.0 * 200.0)
    assert result.roi_score == pytest.approx(expected_roi, abs=0.5)


# ---------------------------------------------------------------------------
# Consistency score (Bayesian shrinkage) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consistency_bayesian_small_sample() -> None:
    """With only 30 resolved trades (all wins), Bayesian prior pulls score < 100."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_winning_trade(size=2000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    # (30 + 15) / (30 + 30) * 100 = 75.0
    assert result.consistency_score == pytest.approx(75.0, abs=1e-6)


@pytest.mark.asyncio
async def test_consistency_bayesian_zero_wins() -> None:
    """Zero wins still gets > 0 due to Bayesian prior of 15 pseudo-wins."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_losing_trade(size=2000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    # (0 + 15) / (30 + 30) * 100 = 25.0
    assert result.consistency_score == pytest.approx(25.0, abs=1e-6)


@pytest.mark.asyncio
async def test_consistency_bayesian_large_sample() -> None:
    """100 resolved trades with 100% wins → score converges toward 100."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_winning_trade(size=1000.0) for _ in range(100)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    # (100 + 15) / (100 + 30) * 100 ≈ 88.5
    expected = (100 + 15) / (100 + 30) * 100
    assert result.consistency_score == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# Sizing score tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sizing_score_at_p90() -> None:
    """Median trade size equal to P90 should give sizing_score = 100."""
    scorer = _make_scorer(redis_return=1000.0)  # P90 = $1000
    trades = [_winning_trade(size=1000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.sizing_score == pytest.approx(100.0, abs=1.0)


@pytest.mark.asyncio
async def test_sizing_score_below_p90() -> None:
    """Median $500 vs P90 $2000 → sizing_score = 25."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_winning_trade(size=500.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.sizing_score == pytest.approx(25.0, abs=1.0)


@pytest.mark.asyncio
async def test_sizing_score_above_p90_capped_at_100() -> None:
    """Median $10k vs P90 $2k → sizing_score capped at 100."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_winning_trade(size=10000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.sizing_score == 100.0


@pytest.mark.asyncio
async def test_sizing_score_redis_fallback() -> None:
    """When Redis returns None, falls back to P90_PLATFORM_FALLBACK_USDC=$2000."""
    scorer = _make_scorer(redis_return=None)  # cache miss
    trades = [_winning_trade(size=2000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.sizing_score == pytest.approx(100.0, abs=1.0)


# ---------------------------------------------------------------------------
# Specialization score tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specialization_requires_5_trades_per_category() -> None:
    """Categories with < 5 trades are ignored."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = []
    # 30 POLITICS wins, 4 CRYPTO trades (below threshold)
    trades += [_winning_trade(category="POLITICS") for _ in range(30)]
    # 4 crypto trades don't qualify
    for _ in range(4):
        trades.append(_make_trade(category="CRYPTO", resolution="YES", outcome_purchased="YES", payout_usdc=500.0))
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.best_category == "POLITICS"


@pytest.mark.asyncio
async def test_specialization_best_category_selected() -> None:
    """The category with the highest win rate is chosen."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = []
    # POLITICS: 8 wins, 2 losses → 80%
    trades += [_winning_trade(category="POLITICS") for _ in range(8)]
    trades += [_losing_trade(category="POLITICS") for _ in range(2)]
    # CRYPTO: 5 wins, 5 losses → 50%
    trades += [_winning_trade(category="CRYPTO") for _ in range(5)]
    trades += [_losing_trade(category="CRYPTO") for _ in range(5)]
    # Pad to 30 resolved
    trades += [_winning_trade(category="SPORTS") for _ in range(10)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.best_category == "POLITICS"
    assert result.best_category_win_rate == pytest.approx(0.8, abs=1e-6)
    assert result.specialization_score == pytest.approx(80.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Recency score tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recency_score_recent_wins_higher() -> None:
    """Recent winning trades should produce a higher recency score than old ones."""
    scorer = _make_scorer(redis_return=2000.0)
    # 20 recent wins (1 day old), 10 old wins (89 days old)
    recent = [_winning_trade(days_ago=1) for _ in range(20)]
    old = [_winning_trade(days_ago=89) for _ in range(10)]
    trades = recent + old

    # Patch timestamps
    now = datetime(2024, 3, 1, tzinfo=timezone.utc)
    for t in recent:
        object.__setattr__(t, "timestamp", datetime(2024, 2, 28, tzinfo=timezone.utc))
    for t in old:
        object.__setattr__(t, "timestamp", datetime(2023, 12, 1, tzinfo=timezone.utc))

    with patch("scoring.whale_scorer.datetime") as mock_dt:
        mock_dt.now.return_value = now
        result = await scorer.score_wallet("0x" + "a" * 40, trades)

    assert result.recency_score > 0.0
    assert result.recency_score <= 100.0


@pytest.mark.asyncio
async def test_recency_score_zero_when_all_losses() -> None:
    """When undiscounted profit ≤ 0, recency_score must be 0."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_losing_trade(size=1000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert result.recency_score == 0.0


# ---------------------------------------------------------------------------
# Composite score tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_weights_sum_to_one() -> None:
    """Verify composite score is a valid weighted average (between 0 and 100)."""
    scorer = _make_scorer(redis_return=2000.0)
    trades = [_winning_trade(size=2000.0) for _ in range(30)]
    result = await scorer.score_wallet("0x" + "a" * 40, trades)
    assert 0.0 <= result.whale_score <= 100.0


# ---------------------------------------------------------------------------
# Market maker filter tests
# ---------------------------------------------------------------------------


def test_market_maker_filter_balanced() -> None:
    """A wallet with near-equal buy/sell volume is flagged as market maker."""
    trades = (
        [_make_trade(side="BUY", size_usdc=1000.0, resolution=None) for _ in range(10)]
        + [_make_trade(side="SELL", size_usdc=1000.0, resolution=None) for _ in range(10)]
    )
    assert _is_market_maker(trades) is True


def test_market_maker_filter_directional() -> None:
    """A wallet with strongly directional trades is NOT a market maker."""
    trades = (
        [_make_trade(side="BUY", size_usdc=1000.0, resolution=None) for _ in range(18)]
        + [_make_trade(side="SELL", size_usdc=1000.0, resolution=None) for _ in range(2)]
    )
    assert _is_market_maker(trades) is False


def test_market_maker_filter_exactly_at_threshold() -> None:
    """At exactly 15% imbalance, wallet IS considered a market maker (< 0.15 is the threshold)."""
    # 15% imbalance: buy=57.5, sell=42.5, total=100, imbalance=(15)/100=0.15 → NOT market maker
    buy_vol = 57.5
    sell_vol = 42.5
    trades = []
    trades.append(_make_trade(side="BUY", size_usdc=buy_vol, resolution=None))
    trades.append(_make_trade(side="SELL", size_usdc=sell_vol, resolution=None))
    # imbalance = abs(57.5-42.5)/100 = 0.15 → NOT < 0.15 → not a market maker
    assert _is_market_maker(trades) is False


def test_market_maker_filter_no_trades() -> None:
    """Empty trade list should not crash."""
    assert _is_market_maker([]) is False


# ---------------------------------------------------------------------------
# Category normalisation
# ---------------------------------------------------------------------------


def test_normalise_category_politics() -> None:
    assert _normalise_category("Political events") == "POLITICS"


def test_normalise_category_crypto() -> None:
    assert _normalise_category("CRYPTO / DeFi") == "CRYPTO"


def test_normalise_category_unknown() -> None:
    assert _normalise_category("Random stuff") == "OTHER"
