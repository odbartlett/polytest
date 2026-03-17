"""Tests for the position ledger — trade classification logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from signals.position_ledger import PositionLedger, TradeClassification, WalletMarketPosition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_redis() -> AsyncMock:
    """Mock Redis client that uses an in-memory dict for hset/hgetall/expire/delete."""
    store: dict[str, dict[str, str]] = {}

    redis = AsyncMock()

    async def hset(key: str, mapping: dict[str, str]) -> None:
        store[key] = dict(mapping)

    async def hgetall(key: str) -> dict[str, str]:
        return dict(store.get(key, {}))

    async def expire(key: str, ttl: int) -> None:
        pass

    async def delete(key: str) -> None:
        store.pop(key, None)

    redis.hset = AsyncMock(side_effect=hset)
    redis.hgetall = AsyncMock(side_effect=hgetall)
    redis.expire = AsyncMock(side_effect=expire)
    redis.delete = AsyncMock(side_effect=delete)

    return redis


WALLET = "0xwhale"
MARKET = "market_abc"
TOKEN = "token_yes"


# ---------------------------------------------------------------------------
# ENTRY classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_on_first_buy() -> None:
    """First BUY in a market should be classified as ENTRY."""
    ledger = PositionLedger(redis_client=_make_redis())
    classification = await ledger.update(
        wallet=WALLET, market_id=MARKET, token_id=TOKEN,
        side="BUY", size=500.0, price=0.60,
    )
    assert classification == TradeClassification.ENTRY


@pytest.mark.asyncio
async def test_entry_creates_position() -> None:
    """After an ENTRY, get_position should return the new position."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(
        wallet=WALLET, market_id=MARKET, token_id=TOKEN,
        side="BUY", size=500.0, price=0.60,
    )
    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is not None
    assert pos.side == "BUY"
    assert pos.size == pytest.approx(500.0)
    assert pos.avg_price == pytest.approx(0.60)


# ---------------------------------------------------------------------------
# ADD classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_on_second_same_direction_buy() -> None:
    """A follow-on BUY in the same market/direction should be ADD."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 500.0, 0.60)
    classification = await ledger.update(WALLET, MARKET, TOKEN, "BUY", 300.0, 0.65)
    assert classification == TradeClassification.ADD


@pytest.mark.asyncio
async def test_add_updates_vwap_avg_price() -> None:
    """ADD should update avg_price as VWAP."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 500.0, 0.60)  # cost = 300
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 500.0, 0.70)  # cost = 350

    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is not None
    # VWAP = (500*0.60 + 500*0.70) / 1000 = (300 + 350) / 1000 = 0.65
    assert pos.avg_price == pytest.approx(0.65, abs=1e-6)
    assert pos.size == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# EXIT classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_on_partial_sell() -> None:
    """A SELL smaller than current position should be EXIT."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 1000.0, 0.60)
    classification = await ledger.update(WALLET, MARKET, TOKEN, "SELL", 400.0, 0.70)
    assert classification == TradeClassification.EXIT


@pytest.mark.asyncio
async def test_exit_reduces_position_size() -> None:
    """EXIT should reduce the position size appropriately."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 1000.0, 0.60)
    await ledger.update(WALLET, MARKET, TOKEN, "SELL", 400.0, 0.70)

    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is not None
    assert pos.size == pytest.approx(600.0)


@pytest.mark.asyncio
async def test_full_exit_removes_position() -> None:
    """Selling the entire position should remove it from the ledger."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 1000.0, 0.60)
    await ledger.update(WALLET, MARKET, TOKEN, "SELL", 1000.0, 0.80)

    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is None


# ---------------------------------------------------------------------------
# FLIP classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_classification() -> None:
    """A SELL larger than current position triggers a FLIP."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 1000.0, 0.60)

    # classify_trade_with_size: sell of 2000 > current 1000 → FLIP
    classification = await ledger.classify_trade_with_size(
        wallet=WALLET,
        market_id=MARKET,
        token_id=TOKEN,
        side="SELL",
        size_usdc=2000.0,
    )
    assert classification == TradeClassification.FLIP


@pytest.mark.asyncio
async def test_flip_replaces_position_with_opposite_side() -> None:
    """After a FLIP, the position should be in the new direction."""
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 1000.0, 0.60)
    # Force a FLIP by calling update with SELL after classifying as FLIP
    # Simulate by directly calling update
    await ledger.update(WALLET, MARKET, TOKEN, "SELL", 2000.0, 0.30)

    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is not None
    assert pos.side == "SELL"
    assert pos.size == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# NOISE classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noise_classification_for_tiny_trades() -> None:
    """Trades below NOISE_THRESHOLD_USDC should be classified as NOISE."""
    ledger = PositionLedger(redis_client=_make_redis())
    classification = await ledger.classify_trade_with_size(
        wallet=WALLET, market_id=MARKET, token_id=TOKEN, side="BUY", size_usdc=5.0
    )
    assert classification == TradeClassification.NOISE


@pytest.mark.asyncio
async def test_noise_trade_does_not_create_position() -> None:
    """A NOISE trade must not create any position entry."""
    ledger = PositionLedger(redis_client=_make_redis())
    noise_classification = await ledger.classify_trade_with_size(
        wallet=WALLET, market_id=MARKET, token_id=TOKEN, side="BUY", size_usdc=1.0
    )
    assert noise_classification == TradeClassification.NOISE

    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is None


# ---------------------------------------------------------------------------
# Redis fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_failure_falls_back_to_memory() -> None:
    """When Redis raises, the ledger should fall back to in-memory storage."""
    broken_redis = AsyncMock()
    broken_redis.hgetall.side_effect = ConnectionError("Redis down")
    broken_redis.hset.side_effect = ConnectionError("Redis down")
    broken_redis.expire.side_effect = ConnectionError("Redis down")
    broken_redis.delete.side_effect = ConnectionError("Redis down")

    ledger = PositionLedger(redis_client=broken_redis)
    # Should not raise
    classification = await ledger.update(WALLET, MARKET, TOKEN, "BUY", 500.0, 0.60)
    assert classification == TradeClassification.ENTRY

    # Should return from in-memory fallback
    pos = await ledger.get_position(WALLET, MARKET)
    assert pos is not None
    assert pos.side == "BUY"


# ---------------------------------------------------------------------------
# classify_trade (direction-only, no size)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_trade_entry_when_no_position() -> None:
    ledger = PositionLedger(redis_client=_make_redis())
    c = await ledger.classify_trade(WALLET, MARKET, TOKEN, "BUY")
    assert c == TradeClassification.ENTRY


@pytest.mark.asyncio
async def test_classify_trade_add_when_same_direction() -> None:
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 500.0, 0.60)
    c = await ledger.classify_trade(WALLET, MARKET, TOKEN, "BUY")
    assert c == TradeClassification.ADD


@pytest.mark.asyncio
async def test_classify_trade_exit_when_opposite_direction() -> None:
    ledger = PositionLedger(redis_client=_make_redis())
    await ledger.update(WALLET, MARKET, TOKEN, "BUY", 500.0, 0.60)
    c = await ledger.classify_trade(WALLET, MARKET, TOKEN, "SELL")
    assert c == TradeClassification.EXIT
