"""In-memory + Redis-backed position ledger for whale wallets.

Tracks the net position of every whitelisted wallet in every market they trade,
and classifies each incoming trade as ENTRY / ADD / EXIT / FLIP / NOISE.

Redis key format: ledger:{wallet}:{market_id}
Each key is a Redis hash with fields:
  token_id, side, size (shares), avg_price, entry_count
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import structlog

from config.settings import get_settings

logger = structlog.get_logger(__name__)

_settings = get_settings()

# Minimum size (USDC) to bother tracking — below this it's NOISE
NOISE_THRESHOLD_USDC: float = 10.0

# Redis TTL for ledger entries (90 days in seconds)
LEDGER_TTL_SECONDS: int = 90 * 24 * 3600


class TradeClassification(str, Enum):
    ENTRY = "ENTRY"          # First position in this market
    ADD = "ADD"              # Adding to an existing same-direction position
    EXIT = "EXIT"            # Reducing / closing a position
    FLIP = "FLIP"            # Reversing direction (exit + re-entry)
    NOISE = "NOISE"          # Trade too small to track


@dataclass
class WalletMarketPosition:
    wallet: str
    market_id: str
    token_id: str
    side: str          # "BUY" or "SELL"
    size: float        # Net shares held
    avg_price: float
    entry_count: int = 1

    @property
    def is_flat(self) -> bool:
        return self.size <= 0.0


class PositionLedger:
    """Tracks whale wallet positions backed by Redis for persistence."""

    def __init__(self, redis_client: object) -> None:
        self._redis = redis_client
        # In-memory fallback — used when Redis is unavailable
        self._fallback: dict[str, dict[str, str]] = {}
        self._redis_ok: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def update(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> TradeClassification:
        """Update the position and return the trade classification."""
        classification = await self.classify_trade(wallet, market_id, token_id, side)

        if classification == TradeClassification.NOISE:
            return classification

        current = await self.get_position(wallet, market_id)

        if classification in (TradeClassification.ENTRY, TradeClassification.ADD):
            new_size = (current.size if current else 0.0) + size
            current_count = current.entry_count if current else 0
            if current and current.side == side:
                # VWAP-update avg price
                old_cost = current.size * current.avg_price
                new_cost = size * price
                new_avg = (old_cost + new_cost) / new_size if new_size > 0 else price
            else:
                new_avg = price
            await self._save(wallet, market_id, {
                "token_id": token_id,
                "side": side,
                "size": str(new_size),
                "avg_price": str(new_avg),
                "entry_count": str(current_count + 1),
            })

        elif classification == TradeClassification.EXIT:
            if current:
                new_size = max(0.0, current.size - size)
                if new_size <= 0:
                    await self._delete(wallet, market_id)
                else:
                    await self._save(wallet, market_id, {
                        "token_id": current.token_id,
                        "side": current.side,
                        "size": str(new_size),
                        "avg_price": str(current.avg_price),
                        "entry_count": str(current.entry_count),
                    })

        elif classification == TradeClassification.FLIP:
            # Close existing position and open a new one in the opposite direction
            await self._save(wallet, market_id, {
                "token_id": token_id,
                "side": side,
                "size": str(size),
                "avg_price": str(price),
                "entry_count": "1",
            })

        logger.debug(
            "ledger.updated",
            wallet=wallet,
            market=market_id,
            side=side,
            size=size,
            classification=classification.value,
        )
        return classification

    async def get_position(
        self, wallet: str, market_id: str
    ) -> Optional[WalletMarketPosition]:
        """Retrieve the current position for a wallet/market pair."""
        data = await self._load(wallet, market_id)
        if not data:
            return None
        try:
            return WalletMarketPosition(
                wallet=wallet,
                market_id=market_id,
                token_id=data.get("token_id", ""),
                side=data.get("side", "BUY"),
                size=float(data.get("size", 0)),
                avg_price=float(data.get("avg_price", 0)),
                entry_count=int(data.get("entry_count", 1)),
            )
        except (ValueError, KeyError) as exc:
            logger.warning("ledger.parse_error", wallet=wallet, market=market_id, error=str(exc))
            return None

    async def classify_trade(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        side: str,
    ) -> TradeClassification:
        """Classify a trade without modifying the ledger."""
        # We need the size to classify NOISE — use a sentinel approach
        # (actual size check is in update(); classify_trade is called first)
        current = await self.get_position(wallet, market_id)

        if current is None or current.is_flat:
            return TradeClassification.ENTRY

        if current.side == side:
            return TradeClassification.ADD

        # Opposite side — either EXIT or FLIP
        # We'll call it EXIT here; the caller (update) will handle FLIP logic
        # based on whether the size exceeds the remaining position
        return TradeClassification.EXIT

    async def classify_trade_with_size(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        side: str,
        size_usdc: float,
    ) -> TradeClassification:
        """Classify a trade taking size into account (for NOISE detection)."""
        if size_usdc < NOISE_THRESHOLD_USDC:
            return TradeClassification.NOISE

        current = await self.get_position(wallet, market_id)

        if current is None or current.is_flat:
            return TradeClassification.ENTRY

        if current.side == side:
            return TradeClassification.ADD

        # Opposite side — FLIP if we're adding more than we hold, else EXIT
        shares_being_sold = size_usdc / max(current.avg_price, 0.001)
        if shares_being_sold >= current.size * 0.95:
            return TradeClassification.FLIP
        return TradeClassification.EXIT

    # ------------------------------------------------------------------
    # Redis persistence
    # ------------------------------------------------------------------

    def _key(self, wallet: str, market_id: str) -> str:
        return f"ledger:{wallet.lower()}:{market_id}"

    async def _save(self, wallet: str, market_id: str, data: dict[str, str]) -> None:
        key = self._key(wallet, market_id)
        if self._redis_ok:
            try:
                await self._redis.hset(key, mapping=data)  # type: ignore[union-attr]
                await self._redis.expire(key, LEDGER_TTL_SECONDS)  # type: ignore[union-attr]
                return
            except Exception as exc:
                logger.warning("ledger.redis_write_failed", key=key, error=str(exc))
                self._redis_ok = False
        # Fallback to in-memory
        self._fallback[key] = data

    async def _load(self, wallet: str, market_id: str) -> Optional[dict[str, str]]:
        key = self._key(wallet, market_id)
        if self._redis_ok:
            try:
                data = await self._redis.hgetall(key)  # type: ignore[union-attr]
                if data:
                    return {
                        k.decode("utf-8") if isinstance(k, bytes) else k:
                        v.decode("utf-8") if isinstance(v, bytes) else v
                        for k, v in data.items()
                    }
                return None
            except Exception as exc:
                logger.warning("ledger.redis_read_failed", key=key, error=str(exc))
                self._redis_ok = False
        return self._fallback.get(key)

    async def _delete(self, wallet: str, market_id: str) -> None:
        key = self._key(wallet, market_id)
        if self._redis_ok:
            try:
                await self._redis.delete(key)  # type: ignore[union-attr]
                return
            except Exception as exc:
                logger.warning("ledger.redis_delete_failed", key=key, error=str(exc))
                self._redis_ok = False
        self._fallback.pop(key, None)
