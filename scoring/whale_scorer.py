"""Whale score calculation engine.

Computes a composite whale score for a wallet based on five components:
  roi_score          (weight 0.35)
  consistency_score  (weight 0.25)
  sizing_score       (weight 0.20)
  specialization_score (weight 0.10)
  recency_score      (weight 0.10)

All inputs are HistoricalTrade objects from the Bitquery client.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Optional

import structlog
from pydantic import BaseModel

from config.settings import get_settings
from data.bitquery_client import HistoricalTrade

logger = structlog.get_logger(__name__)

_settings = get_settings()

# Category normalisation map
CATEGORY_NORMALISE: dict[str, str] = {
    "politics": "POLITICS",
    "political": "POLITICS",
    "crypto": "CRYPTO",
    "cryptocurrency": "CRYPTO",
    "defi": "CRYPTO",
    "sports": "SPORTS",
    "sport": "SPORTS",
    "economics": "ECONOMICS",
    "economy": "ECONOMICS",
    "finance": "ECONOMICS",
    "science": "SCIENCE_GEO",
    "geo": "SCIENCE_GEO",
    "geography": "SCIENCE_GEO",
    "climate": "SCIENCE_GEO",
}

# Platform-wide P90 trade size fallback (used when Redis cache is unavailable)
P90_PLATFORM_FALLBACK_USDC = 2_000.0

# Scoring weights
WEIGHTS = {
    "roi": 0.35,
    "consistency": 0.25,
    "sizing": 0.20,
    "specialization": 0.10,
    "recency": 0.10,
}


class InsufficientDataError(Exception):
    """Raised when a wallet lacks enough trades to be scored."""


class WalletScoreResult(BaseModel):
    """Computed scoring result — mirrors the WalletScore ORM model fields."""

    wallet_address: str
    whale_score: float
    roi_score: float
    consistency_score: float
    sizing_score: float
    specialization_score: float
    recency_score: float
    total_volume_usdc: float
    resolved_markets_count: int
    win_count: int
    best_category: Optional[str]
    best_category_win_rate: Optional[float]
    last_scored_at: datetime


class WhaleScorerService:
    """Computes whale scores from a wallet's historical trade list."""

    def __init__(self, redis_client: Optional[object] = None) -> None:
        """
        Args:
            redis_client: Optional aioredis client for reading cached P90 sizes.
        """
        self._redis = redis_client
        self._settings = get_settings()

    async def score_wallet(
        self,
        wallet_address: str,
        trades: list[HistoricalTrade],
    ) -> WalletScoreResult:
        """Compute a composite whale score for the given wallet.

        Raises:
            InsufficientDataError: When minimum thresholds are not met.
        """
        if not trades:
            raise InsufficientDataError(
                f"No trades provided for {wallet_address}"
            )

        total_volume = sum(t.size_usdc for t in trades)
        if total_volume < self._settings.MIN_TOTAL_VOLUME_USDC:
            raise InsufficientDataError(
                f"Volume {total_volume:.0f} USDC < minimum {self._settings.MIN_TOTAL_VOLUME_USDC}"
            )
        if len(trades) < self._settings.MIN_TRADE_COUNT:
            raise InsufficientDataError(
                f"Only {len(trades)} trades — minimum is {self._settings.MIN_TRADE_COUNT}"
            )

        resolved = [t for t in trades if t.resolution is not None]
        if len(resolved) < self._settings.MIN_RESOLVED_MARKETS:
            raise InsufficientDataError(
                f"Only {len(resolved)} resolved markets — minimum is {self._settings.MIN_RESOLVED_MARKETS}"
            )

        wins = [t for t in resolved if t.is_winner]
        win_count = len(wins)

        roi_score = self._compute_roi_score(resolved)
        consistency_score = self._compute_consistency_score(win_count, len(resolved))
        sizing_score = await self._compute_sizing_score(trades)
        specialization_score, best_category, best_cat_win_rate = (
            self._compute_specialization_score(resolved)
        )
        recency_score = self._compute_recency_score(resolved)

        composite = (
            WEIGHTS["roi"] * roi_score
            + WEIGHTS["consistency"] * consistency_score
            + WEIGHTS["sizing"] * sizing_score
            + WEIGHTS["specialization"] * specialization_score
            + WEIGHTS["recency"] * recency_score
        )
        whale_score = round(min(100.0, max(0.0, composite)), 4)

        result = WalletScoreResult(
            wallet_address=wallet_address,
            whale_score=whale_score,
            roi_score=round(roi_score, 4),
            consistency_score=round(consistency_score, 4),
            sizing_score=round(sizing_score, 4),
            specialization_score=round(specialization_score, 4),
            recency_score=round(recency_score, 4),
            total_volume_usdc=round(total_volume, 2),
            resolved_markets_count=len(resolved),
            win_count=win_count,
            best_category=best_category,
            best_category_win_rate=round(best_cat_win_rate, 4) if best_cat_win_rate is not None else None,
            last_scored_at=datetime.now(tz=timezone.utc),
        )

        logger.info(
            "scorer.wallet_scored",
            wallet=wallet_address,
            whale_score=whale_score,
            roi=roi_score,
            consistency=consistency_score,
            sizing=sizing_score,
            specialization=specialization_score,
            recency=recency_score,
        )
        return result

    # ------------------------------------------------------------------
    # Component scorers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_roi_score(resolved: list[HistoricalTrade]) -> float:
        """ROI score: min(100, max(0, realized_roi * 200)).

        realized_roi = (total_payout - total_cost) / total_cost
        Only resolved trades are included.
        """
        total_cost = sum(t.size_usdc for t in resolved)
        if total_cost <= 0:
            return 0.0
        total_payout = sum(t.payout_usdc for t in resolved if t.is_winner)
        realized_roi = (total_payout - total_cost) / total_cost
        return float(min(100.0, max(0.0, realized_roi * 200.0)))

    @staticmethod
    def _compute_consistency_score(wins: int, resolved: int) -> float:
        """Bayesian-adjusted win rate: (wins + 15) / (resolved + 30) * 100."""
        return float((wins + 15) / (resolved + 30) * 100.0)

    async def _compute_sizing_score(self, trades: list[HistoricalTrade]) -> float:
        """Sizing score based on median trade size vs. platform P90.

        Falls back to P90_PLATFORM_FALLBACK_USDC if Redis is unavailable.
        """
        if not trades:
            return 0.0
        sizes = [t.size_usdc for t in trades]
        median_size = statistics.median(sizes)
        p90 = await self._get_p90_platform_size()
        return float(min(100.0, (median_size / p90) * 100.0))

    @staticmethod
    def _compute_specialization_score(
        resolved: list[HistoricalTrade],
    ) -> tuple[float, Optional[str], Optional[float]]:
        """Per-category win rate. Requires ≥ 5 trades per category.

        Returns:
            (score, best_category, best_category_win_rate)
        """
        category_stats: dict[str, dict[str, int]] = {}
        for t in resolved:
            cat = _normalise_category(t.category)
            if cat not in category_stats:
                category_stats[cat] = {"wins": 0, "total": 0}
            category_stats[cat]["total"] += 1
            if t.is_winner:
                category_stats[cat]["wins"] += 1

        best_category: Optional[str] = None
        best_win_rate: float = 0.0
        for cat, stats in category_stats.items():
            if stats["total"] < 5:
                continue
            wr = stats["wins"] / stats["total"]
            if wr > best_win_rate:
                best_win_rate = wr
                best_category = cat

        if best_category is None:
            return 0.0, None, None
        return float(best_win_rate * 100.0), best_category, best_win_rate

    def _compute_recency_score(self, resolved: list[HistoricalTrade]) -> float:
        """Exponentially-decayed profit normalised by undiscounted total.

        recency_score = min(100, normalized_weighted_profit * 100)
        """
        now = datetime.now(tz=timezone.utc)
        lam = self._settings.RECENCY_DECAY_LAMBDA

        weighted_profit = 0.0
        total_profit = 0.0
        for t in resolved:
            profit = t.profit_usdc
            # Ensure timestamp is tz-aware
            ts = t.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            days = max(0.0, (now - ts).total_seconds() / 86400.0)
            weighted_profit += profit * math.exp(-lam * days)
            total_profit += profit

        if total_profit <= 0:
            return 0.0
        normalised = weighted_profit / total_profit
        return float(min(100.0, normalised * 100.0))

    async def _get_p90_platform_size(self) -> float:
        """Fetch platform-wide P90 trade size from Redis (set nightly by whitelist job).

        Falls back to P90_PLATFORM_FALLBACK_USDC on cache miss or Redis error.
        """
        if self._redis is None:
            return P90_PLATFORM_FALLBACK_USDC
        try:
            cached = await self._redis.get("whale:p90_trade_size")  # type: ignore[union-attr]
            if cached is not None:
                return float(cached)
        except Exception as exc:
            logger.warning("scorer.redis.p90_cache_miss", error=str(exc))
        return P90_PLATFORM_FALLBACK_USDC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_category(raw: str) -> str:
    """Map raw category string to a canonical category name."""
    lower = raw.lower().strip()
    for key, normalised in CATEGORY_NORMALISE.items():
        if key in lower:
            return normalised
    return "OTHER"
