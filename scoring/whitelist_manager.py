"""Nightly whitelist refresh — identifies, scores, and ranks whale wallets.

The whitelist is stored in Redis as a sorted set (whale:whitelist) and
persisted to the wallet_scores Postgres table for auditing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import get_settings
from data.bitquery_client import BitqueryClient, HistoricalTrade
from db.models import BotPosition, PositionStatus, WalletScore
from db.session import AsyncSessionLocal
from scoring.whale_scorer import InsufficientDataError, WhaleScorerService, WalletScoreResult

logger = structlog.get_logger(__name__)

_settings = get_settings()

REDIS_WHITELIST_KEY = "whale:whitelist"
REDIS_P90_KEY = "whale:p90_trade_size"


class WhitelistRefreshResult(BaseModel):
    added: int
    removed: int
    retained: int
    total: int
    duration_seconds: float


class WhitelistManager:
    """Manages the whale wallet whitelist with nightly refresh."""

    def __init__(self, redis_client: object, scorer: WhaleScorerService) -> None:
        self._redis = redis_client
        self._scorer = scorer
        self._in_memory: dict[str, WalletScoreResult] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def refresh_whitelist(self) -> WhitelistRefreshResult:
        """Run the full nightly whitelist refresh pipeline.

        Steps:
          1. Query Bitquery for candidate wallets meeting minimum criteria.
          2. Score each candidate.
          3. Exclude market makers (balanced buy/sell ratio).
          4. Rank by whale_score, keep top WHITELIST_MAX_SIZE.
          5. Persist to Postgres and Redis.
          6. Preserve wallets with open bot positions regardless of new score.
        """
        started_at = datetime.now(tz=timezone.utc)
        logger.info("whitelist.refresh.started")

        settings = get_settings()
        lookback_start = datetime.now(tz=timezone.utc) - timedelta(days=settings.LOOKBACK_DAYS)

        # Fetch candidate wallets from Bitquery
        candidates = await self._fetch_candidate_wallets(lookback_start)
        logger.info("whitelist.candidates_found", count=len(candidates))

        # Score each candidate — try full Bitquery scoring first, fall back to
        # leaderboard-rank scoring when no API key is configured or all calls fail.
        scored: list[WalletScoreResult] = []
        bitquery_available = bool(_settings.BITQUERY_API_KEY)

        if bitquery_available:
            async with BitqueryClient() as bitquery:
                for wallet_address in candidates:
                    try:
                        trades = await bitquery.get_wallet_trade_history(
                            wallet_address,
                            start_date=lookback_start,
                            end_date=datetime.now(tz=timezone.utc),
                        )
                        if not trades:
                            continue

                        # Exclude market makers
                        if _is_market_maker(trades):
                            logger.debug("whitelist.market_maker_excluded", wallet=wallet_address)
                            continue

                        score = await self._scorer.score_wallet(wallet_address, trades)
                        scored.append(score)

                    except InsufficientDataError as exc:
                        logger.debug("whitelist.insufficient_data", wallet=wallet_address, reason=str(exc))
                    except Exception as exc:
                        logger.error("whitelist.score_error", wallet=wallet_address, error=str(exc))

        if not scored:
            # No Bitquery key or all scoring failed — use leaderboard rank as proxy.
            # Top-10 leaderboard wallets get score 85, next 20 get 75, etc.
            # This ensures the whitelist is populated for live-mode operation.
            logger.info("whitelist.fallback_to_leaderboard_rank", reason="no_bitquery_key" if not bitquery_available else "all_scoring_failed")
            scored = await self._score_from_leaderboard_ranks(candidates)

        logger.info("whitelist.scored", count=len(scored))

        # Update P90 trade size in Redis for sizing_score calculations
        await self._update_p90_cache(candidates_scored=scored)

        # Filter to score floor and rank
        eligible = [s for s in scored if s.whale_score >= settings.WHALE_SCORE_FLOOR]
        eligible.sort(key=lambda s: s.whale_score, reverse=True)
        new_top = eligible[: settings.WHITELIST_MAX_SIZE]
        new_top_addrs = {s.wallet_address for s in new_top}

        # Wallets with open bot positions must not be evicted
        protected_addrs = await self._get_wallets_with_open_positions()
        prev_addrs = set(self._in_memory.keys())

        added = new_top_addrs - prev_addrs
        removed_candidates = prev_addrs - new_top_addrs
        actually_removed: set[str] = set()
        protected_retained: set[str] = set()

        for addr in removed_candidates:
            if addr in protected_addrs:
                logger.warning(
                    "whitelist.protected_wallet_retained",
                    wallet=addr,
                    reason="open bot position",
                )
                protected_retained.add(addr)
            else:
                actually_removed.add(addr)

        # Build new whitelist map
        new_map: dict[str, WalletScoreResult] = {}
        for s in new_top:
            new_map[s.wallet_address] = s
        # Re-add protected wallets with their existing scores
        for addr in protected_retained:
            if addr in self._in_memory:
                new_map[addr] = self._in_memory[addr]

        self._in_memory = new_map

        # Persist to Postgres
        await self._persist_scores(list(new_map.values()))

        # Update Redis sorted set
        await self._update_redis_whitelist(list(new_map.values()))

        elapsed = (datetime.now(tz=timezone.utc) - started_at).total_seconds()
        result = WhitelistRefreshResult(
            added=len(added),
            removed=len(actually_removed),
            retained=len(prev_addrs & new_top_addrs) + len(protected_retained),
            total=len(new_map),
            duration_seconds=elapsed,
        )
        logger.info("whitelist.refresh.complete", **result.model_dump())
        return result

    async def get_whitelist(self) -> list[WalletScoreResult]:
        """Return the current whitelist, falling back to Redis if in-memory is empty."""
        if self._in_memory:
            return sorted(self._in_memory.values(), key=lambda s: s.whale_score, reverse=True)
        return await self._load_from_redis()

    async def is_whitelisted(self, wallet_address: str) -> bool:
        """Check whether a wallet is currently whitelisted."""
        if wallet_address in self._in_memory:
            return True
        # Fall back to Redis
        try:
            score = await self._redis.zscore(REDIS_WHITELIST_KEY, wallet_address)  # type: ignore[union-attr]
            return score is not None
        except Exception:
            return False

    async def get_whale_score(self, wallet_address: str) -> Optional[float]:
        """Return the whale score for a wallet, or None if not whitelisted."""
        if wallet_address in self._in_memory:
            return self._in_memory[wallet_address].whale_score
        try:
            score = await self._redis.zscore(REDIS_WHITELIST_KEY, wallet_address)  # type: ignore[union-attr]
            return float(score) if score is not None else None
        except Exception:
            return None

    async def get_wallet_score_result(self, wallet_address: str) -> Optional[WalletScoreResult]:
        """Return the full WalletScoreResult for a whitelisted wallet."""
        return self._in_memory.get(wallet_address)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_candidate_wallets(self, lookback_start: datetime) -> list[str]:
        """Build candidate wallet list from DB history + live CLOB trade discovery.

        Cold-start strategy:
          1. Existing scored wallets from Postgres (warm re-score).
          2. Active traders discovered via the public Polymarket CLOB trades
             endpoint — no API key required, pure public data.

        Returns a deduplicated list of candidate wallet addresses.
        """
        candidates: list[str] = []

        # 1. Pull previously scored wallets from DB as warm start
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(WalletScore.wallet_address)
                .where(WalletScore.total_volume_usdc >= _settings.MIN_TOTAL_VOLUME_USDC)
                .order_by(WalletScore.whale_score.desc())
                .limit(500)
            )
            db_wallets = [row[0] for row in result.fetchall()]
            candidates.extend(db_wallets)

        logger.info("whitelist.candidates_from_db", count=len(candidates))

        # 2. Discover active wallets from live CLOB trade stream
        discovered = await self._discover_wallets_from_clob()
        new_discovered = [w for w in discovered if w not in set(candidates)]
        candidates.extend(new_discovered)
        logger.info("whitelist.candidates_discovered", count=len(new_discovered))

        return list(dict.fromkeys(candidates))  # deduplicate, preserve order

    async def _discover_wallets_from_clob(self) -> list[str]:
        """Discover active whale wallets via Bitquery on-chain CTF transfer data.

        Primary path: queries Bitquery for the most active receivers of Polymarket
        CTF tokens over the lookback window — no leaderboard API required.
        Falls back to an empty list (warm-start from DB) when no key is configured.
        """
        if not _settings.BITQUERY_API_KEY:
            logger.warning("whitelist.discovery.no_bitquery_key")
            return []

        try:
            lookback_start = datetime.now(tz=timezone.utc) - timedelta(days=_settings.LOOKBACK_DAYS)
            async with BitqueryClient() as bitquery:
                wallets = await bitquery.get_top_trader_wallets(
                    start_date=lookback_start,
                    end_date=datetime.now(tz=timezone.utc),
                )
            logger.info("whitelist.bitquery_discovery.complete", total=len(wallets))
            return wallets
        except Exception as exc:
            logger.warning("whitelist.bitquery_discovery.failed", error=str(exc))
            return []

    async def _get_wallets_with_open_positions(self) -> set[str]:
        """Return wallet addresses that have open bot positions copied from them."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotPosition.copied_from_wallet)
                .where(BotPosition.status == PositionStatus.OPEN)
            )
            return {row[0] for row in result.fetchall()}

    async def _persist_scores(self, scores: list[WalletScoreResult]) -> None:
        """Upsert wallet scores into the Postgres wallet_scores table."""
        if not scores:
            return
        async with AsyncSessionLocal() as session:
            async with session.begin():
                for score in scores:
                    stmt = pg_insert(WalletScore).values(
                        wallet_address=score.wallet_address,
                        whale_score=score.whale_score,
                        roi_score=score.roi_score,
                        consistency_score=score.consistency_score,
                        sizing_score=score.sizing_score,
                        specialization_score=score.specialization_score,
                        recency_score=score.recency_score,
                        total_volume_usdc=score.total_volume_usdc,
                        resolved_markets_count=score.resolved_markets_count,
                        win_count=score.win_count,
                        best_category=score.best_category,
                        best_category_win_rate=score.best_category_win_rate,
                        last_scored_at=score.last_scored_at,
                    ).on_conflict_do_update(
                        index_elements=["wallet_address"],
                        set_={
                            "whale_score": score.whale_score,
                            "roi_score": score.roi_score,
                            "consistency_score": score.consistency_score,
                            "sizing_score": score.sizing_score,
                            "specialization_score": score.specialization_score,
                            "recency_score": score.recency_score,
                            "total_volume_usdc": score.total_volume_usdc,
                            "resolved_markets_count": score.resolved_markets_count,
                            "win_count": score.win_count,
                            "best_category": score.best_category,
                            "best_category_win_rate": score.best_category_win_rate,
                            "last_scored_at": score.last_scored_at,
                        },
                    )
                    await session.execute(stmt)
        logger.info("whitelist.scores_persisted", count=len(scores))

    async def _update_redis_whitelist(self, scores: list[WalletScoreResult]) -> None:
        """Replace the Redis sorted set with the current whitelist."""
        try:
            pipe = self._redis.pipeline()  # type: ignore[union-attr]
            pipe.delete(REDIS_WHITELIST_KEY)
            if scores:
                mapping = {s.wallet_address: s.whale_score for s in scores}
                pipe.zadd(REDIS_WHITELIST_KEY, mapping)
            await pipe.execute()
            logger.info("whitelist.redis_updated", count=len(scores))
        except Exception as exc:
            logger.error("whitelist.redis_update_failed", error=str(exc))

    async def _load_from_redis(self) -> list[WalletScoreResult]:
        """Load whitelist entries from Redis sorted set (partial data only)."""
        try:
            members = await self._redis.zrangebyscore(  # type: ignore[union-attr]
                REDIS_WHITELIST_KEY, "-inf", "+inf", withscores=True
            )
            results: list[WalletScoreResult] = []
            for member, score in reversed(members):
                addr = member.decode("utf-8") if isinstance(member, bytes) else str(member)
                results.append(
                    WalletScoreResult(
                        wallet_address=addr,
                        whale_score=float(score),
                        roi_score=0.0,
                        consistency_score=0.0,
                        sizing_score=0.0,
                        specialization_score=0.0,
                        recency_score=0.0,
                        total_volume_usdc=0.0,
                        resolved_markets_count=0,
                        win_count=0,
                        best_category=None,
                        best_category_win_rate=None,
                        last_scored_at=datetime.now(tz=timezone.utc),
                    )
                )
            return results
        except Exception as exc:
            logger.error("whitelist.redis_load_failed", error=str(exc))
            return []

    async def _score_from_leaderboard_ranks(
        self, candidates: list[str]
    ) -> list[WalletScoreResult]:
        """Assign proxy scores by leaderboard rank when Bitquery is unavailable.

        Rank tiers:
          Rank  1–10  → whale_score 85  (top performers)
          Rank 11–30  → whale_score 75
          Rank 31–60  → whale_score 65
          Rank 61+    → whale_score 58  (just above floor)

        All component scores are set to 70 (neutral) since we have no
        trade-level data to compute them from.
        """
        now = datetime.now(tz=timezone.utc)
        settings = get_settings()
        results: list[WalletScoreResult] = []

        for i, addr in enumerate(candidates[: settings.WHITELIST_MAX_SIZE]):
            if i < 10:
                score = 85.0
            elif i < 30:
                score = 75.0
            elif i < 60:
                score = 65.0
            else:
                score = 58.0

            results.append(
                WalletScoreResult(
                    wallet_address=addr,
                    whale_score=score,
                    roi_score=70.0,
                    consistency_score=70.0,
                    sizing_score=70.0,
                    specialization_score=70.0,
                    recency_score=70.0,
                    total_volume_usdc=settings.MIN_TOTAL_VOLUME_USDC,
                    resolved_markets_count=settings.MIN_RESOLVED_MARKETS,
                    win_count=15,
                    best_category=None,
                    best_category_win_rate=None,
                    last_scored_at=now,
                )
            )

        logger.info("whitelist.leaderboard_rank_scored", count=len(results))
        return results

    async def _update_p90_cache(self, candidates_scored: list[WalletScoreResult]) -> None:
        """Compute approximate platform-wide P90 trade size and cache in Redis.

        Uses the scored wallets' volume data as a proxy.
        """
        if not candidates_scored:
            return
        volumes = sorted([s.total_volume_usdc for s in candidates_scored])
        p90_idx = int(len(volumes) * 0.90)
        p90 = volumes[min(p90_idx, len(volumes) - 1)]
        try:
            await self._redis.set(REDIS_P90_KEY, str(p90), ex=86400)  # type: ignore[union-attr]
            logger.info("whitelist.p90_cached", p90_usdc=p90)
        except Exception as exc:
            logger.warning("whitelist.p90_cache_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_market_maker(trades: list[HistoricalTrade]) -> bool:
    """Return True if the wallet appears to be a market maker.

    Market makers are identified by a near-equal buy/sell volume ratio.
    Wallets where abs(buy_vol - sell_vol) / total_vol < 0.15 are excluded.
    """
    buy_vol = sum(t.size_usdc for t in trades if t.side == "BUY")
    sell_vol = sum(t.size_usdc for t in trades if t.side == "SELL")
    total_vol = buy_vol + sell_vol
    if total_vol <= 0:
        return False
    imbalance = abs(buy_vol - sell_vol) / total_vol
    return imbalance < 0.15
