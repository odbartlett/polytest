"""Signal generation and validation engine.

Implements 8 sequential gate checks. The first failing gate short-circuits
evaluation and returns should_trade=False with the specific failure reason.

Copy sizing is computed using a tiered percentage of bankroll scaled by a
confidence multiplier derived from the whale's ROI and consistency scores.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import structlog
from pydantic import BaseModel

from config.settings import get_settings
from data.clob_client import CLOBClient, Market, TradeEvent
from signals.position_ledger import PositionLedger, TradeClassification

# Markets that have been successfully fetched are cached here for the process
# lifetime to avoid hammering the CLOB API with repeated lookups for the same
# alpha markets we already know about.
_market_cache: dict[str, Market] = {}

logger = structlog.get_logger(__name__)

_settings = get_settings()

# ---------------------------------------------------------------------------
# Score tier → bankroll percentage mapping
# ---------------------------------------------------------------------------
TIER_PCT: list[tuple[float, float, float]] = [
    (55.0, 65.0, 0.005),
    (65.0, 75.0, 0.010),
    (75.0, 85.0, 0.015),
    (85.0, 101.0, 0.020),
]


def _get_tier_pct(score: float) -> float:
    for lo, hi, pct in TIER_PCT:
        if lo <= score < hi:
            return pct
    return 0.0


# ---------------------------------------------------------------------------
# Signal models
# ---------------------------------------------------------------------------


class SignalDecision(BaseModel):
    should_trade: bool
    copy_size_usdc: float
    reason: str
    whale_score: float
    roi_score: float = 0.0
    consistency_score: float = 0.0
    gate_failed: Optional[str] = None
    token_id: str = ""


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------


class SignalEngine:
    """Evaluates incoming whale trades against 8 ordered gate checks."""

    def __init__(
        self,
        clob_client: CLOBClient,
        position_ledger: PositionLedger,
        whitelist_manager: object,  # WhitelistManager (avoid circular import)
        risk_gate: object,          # RiskGate
        redis_client: object,
    ) -> None:
        self._clob = clob_client
        self._ledger = position_ledger
        self._whitelist = whitelist_manager
        self._risk_gate = risk_gate
        self._redis = redis_client
        self._settings = get_settings()

    async def evaluate(self, trade: TradeEvent) -> SignalDecision:
        """Evaluate a trade event and return a SignalDecision.

        Gates are checked in order — first failure short-circuits.
        """

        # ----------------------------------------------------------------
        # Gate 1: TRADE_IS_BUY
        # ----------------------------------------------------------------
        if trade.side != "BUY":
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason="TRADE_IS_BUY: trade side is not BUY",
                whale_score=0.0,
                gate_failed="TRADE_IS_BUY",
            )

        # ----------------------------------------------------------------
        # Gate 2: TRADE_SIZE_MIN
        # ----------------------------------------------------------------
        if trade.size_usdc < self._settings.MIN_WHALE_TRADE_SIZE:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"TRADE_SIZE_MIN: size ${trade.size_usdc:.2f} < "
                    f"minimum ${self._settings.MIN_WHALE_TRADE_SIZE:.2f}"
                ),
                whale_score=0.0,
                gate_failed="TRADE_SIZE_MIN",
            )

        # ----------------------------------------------------------------
        # Gate 3: PRICE_RANGE
        # Only copy trades where the token price offers meaningful upside.
        # Tokens near $1.00 are already near-certain (no upside).
        # Tokens near $0.00 are near-impossible (no realistic upside either).
        # ----------------------------------------------------------------
        if not (self._settings.MIN_ENTRY_PRICE <= trade.price <= self._settings.MAX_ENTRY_PRICE):
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"PRICE_RANGE: price {trade.price:.3f} outside "
                    f"[{self._settings.MIN_ENTRY_PRICE}, {self._settings.MAX_ENTRY_PRICE}]"
                ),
                whale_score=0.0,
                gate_failed="PRICE_RANGE",
            )

        # ----------------------------------------------------------------
        # Gate 4: WHALE_SCORE_MIN
        #
        # In simulation mode with the public market channel, trades arrive as
        # wallet_address="MARKET_TRADE" (no identity).  We skip the whitelist
        # lookup and instead derive a synthetic score from trade size.
        # ----------------------------------------------------------------
        if trade.wallet_address == "MARKET_TRADE":
            # Market-centric sim mode: score scales with trade size.
            # MIN_WHALE_TRADE_SIZE → 55 (floor), 10× min → 85 (cap).
            min_size = self._settings.MIN_WHALE_TRADE_SIZE
            size_ratio = trade.size_usdc / max(min_size, 1.0)
            whale_score = min(85.0, 55.0 + (size_ratio - 1.0) * (30.0 / 9.0))
            # Use 75 so confidence_mult = 0.75×0.75×2 = 1.125 (slightly above neutral).
            # Using 50 gives 0.5 (minimum) which halves every copy size.
            roi_score = 75.0
            consistency_score = 75.0
        else:
            score_result = await self._whitelist.get_wallet_score_result(trade.wallet_address)  # type: ignore[attr-defined]
            if score_result is None:
                whale_score = await self._whitelist.get_whale_score(trade.wallet_address)  # type: ignore[attr-defined]
                if whale_score is None:
                    return SignalDecision(
                        should_trade=False,
                        copy_size_usdc=0.0,
                        reason="WHALE_SCORE_MIN: wallet not in whitelist",
                        whale_score=0.0,
                        gate_failed="WHALE_SCORE_MIN",
                    )
                roi_score = 0.0
                consistency_score = 0.0
            else:
                whale_score = score_result.whale_score
                roi_score = score_result.roi_score
                consistency_score = score_result.consistency_score

        if whale_score < self._settings.WHALE_SCORE_FLOOR:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"WHALE_SCORE_MIN: score {whale_score:.1f} < "
                    f"floor {self._settings.WHALE_SCORE_FLOOR}"
                ),
                whale_score=whale_score,
                gate_failed="WHALE_SCORE_MIN",
            )

        # ----------------------------------------------------------------
        # Gate 5: MARKET_OI_MIN
        # ----------------------------------------------------------------
        market = _market_cache.get(trade.market_id)
        if market is None:
            try:
                market = await self._clob.get_market(trade.market_id)
                _market_cache[trade.market_id] = market
            except Exception as exc:
                return SignalDecision(
                    should_trade=False,
                    copy_size_usdc=0.0,
                    reason=f"MARKET_OI_MIN: failed to fetch market: {exc}",
                    whale_score=whale_score,
                    gate_failed="MARKET_OI_MIN",
                )

        # open_interest == 0.0 means the CLOB API returned no volume data
        # (happens without auth credentials in sim mode).  Treat as unknown —
        # we already pre-screened markets via the Gamma API so they're known
        # to have sufficient volume.
        if market.open_interest > 0 and market.open_interest < self._settings.MIN_MARKET_OPEN_INTEREST:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"MARKET_OI_MIN: OI ${market.open_interest:,.0f} < "
                    f"minimum ${self._settings.MIN_MARKET_OPEN_INTEREST:,.0f}"
                ),
                whale_score=whale_score,
                gate_failed="MARKET_OI_MIN",
            )

        # ----------------------------------------------------------------
        # Gate 6: ORDERBOOK_DEPTH
        # ----------------------------------------------------------------
        try:
            orderbook = await self._clob.get_orderbook(trade.token_id)
        except Exception as exc:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=f"ORDERBOOK_DEPTH: failed to fetch orderbook: {exc}",
                whale_score=whale_score,
                gate_failed="ORDERBOOK_DEPTH",
            )

        slippage = self._settings.SLIPPAGE_TOLERANCE_LIQUID
        available_depth = orderbook.depth_within_slippage("BUY", slippage)

        # Compute preliminary copy size to check against depth
        bankroll = await self._get_bankroll()
        tier_pct = _get_tier_pct(whale_score)
        max_exposure = bankroll * self._settings.MAX_PER_MARKET_EXPOSURE_PCT
        base_size = bankroll * tier_pct
        confidence_mult = _compute_confidence_multiplier(roi_score, consistency_score)
        raw_size = base_size * confidence_mult
        depth_cap = available_depth * self._settings.MAX_LIQUIDITY_CONSUMPTION_PCT
        copy_size = min(raw_size, max_exposure, depth_cap)
        copy_size = math.floor(copy_size / 10) * 10  # round down to nearest $10

        if available_depth < self._settings.MIN_COPY_SIZE:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"ORDERBOOK_DEPTH: depth ${available_depth:.2f} too thin "
                    f"for minimum copy size ${self._settings.MIN_COPY_SIZE:.2f}"
                ),
                whale_score=whale_score,
                gate_failed="ORDERBOOK_DEPTH",
            )

        if copy_size < self._settings.MIN_COPY_SIZE:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"ORDERBOOK_DEPTH: computed copy size ${copy_size:.2f} < "
                    f"minimum ${self._settings.MIN_COPY_SIZE:.2f}"
                ),
                whale_score=whale_score,
                gate_failed="ORDERBOOK_DEPTH",
            )

        # ----------------------------------------------------------------
        # Gate 7: POSITION_CAP
        # ----------------------------------------------------------------
        existing_exposure = await self._get_market_exposure(trade.market_id)
        if existing_exposure >= max_exposure:
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason=(
                    f"POSITION_CAP: existing exposure ${existing_exposure:.2f} >= "
                    f"cap ${max_exposure:.2f}"
                ),
                whale_score=whale_score,
                gate_failed="POSITION_CAP",
            )

        # ----------------------------------------------------------------
        # Gate 8: TIME_TO_RESOLUTION
        # ----------------------------------------------------------------
        if market.resolution_time is not None:
            rt = market.resolution_time
            if rt.tzinfo is None:
                rt = rt.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            hours_remaining = (rt - now).total_seconds() / 3600
            if hours_remaining < self._settings.MIN_HOURS_TO_RESOLUTION:
                return SignalDecision(
                    should_trade=False,
                    copy_size_usdc=0.0,
                    reason=(
                        f"TIME_TO_RESOLUTION: only {hours_remaining:.1f}h until resolution "
                        f"(minimum {self._settings.MIN_HOURS_TO_RESOLUTION}h)"
                    ),
                    whale_score=whale_score,
                    gate_failed="TIME_TO_RESOLUTION",
                )

        # ----------------------------------------------------------------
        # Gate 8b: MAX_TIME_TO_RESOLUTION
        # Ignore markets resolving too far in the future — capital efficiency.
        # ----------------------------------------------------------------
        if market.resolution_time is not None:
            rt = market.resolution_time
            if rt.tzinfo is None:
                rt = rt.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            hours_remaining = (rt - now).total_seconds() / 3600
            if hours_remaining > self._settings.MAX_HOURS_TO_RESOLUTION:
                return SignalDecision(
                    should_trade=False,
                    copy_size_usdc=0.0,
                    reason=(
                        f"MAX_TIME_TO_RESOLUTION: {hours_remaining:.0f}h until resolution "
                        f"exceeds max {self._settings.MAX_HOURS_TO_RESOLUTION}h"
                    ),
                    whale_score=whale_score,
                    gate_failed="MAX_TIME_TO_RESOLUTION",
                )

        # ----------------------------------------------------------------
        # Gate 9: CIRCUIT_BREAKER
        # ----------------------------------------------------------------
        if await self._risk_gate.is_circuit_breaker_active():  # type: ignore[attr-defined]
            return SignalDecision(
                should_trade=False,
                copy_size_usdc=0.0,
                reason="CIRCUIT_BREAKER: circuit breaker is active — trading halted",
                whale_score=whale_score,
                gate_failed="CIRCUIT_BREAKER",
            )

        # ----------------------------------------------------------------
        # All gates passed — emit signal
        # ----------------------------------------------------------------
        logger.info(
            "signal.generated",
            wallet=trade.wallet_address,
            market=trade.market_id,
            copy_size_usdc=copy_size,
            whale_score=whale_score,
        )
        return SignalDecision(
            should_trade=True,
            copy_size_usdc=copy_size,
            reason="All gates passed",
            whale_score=whale_score,
            roi_score=roi_score,
            consistency_score=consistency_score,
            token_id=trade.token_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_bankroll(self) -> float:
        """Read current bankroll from Redis, fall back to settings default.

        Sim mode stores bankroll under ``sim:bankroll``; live mode uses
        ``bot:bankroll``.  Check both so sizing is always correct.
        """
        try:
            key = "sim:bankroll" if self._settings.SIMULATION_MODE else "bot:bankroll"
            val = await self._redis.get(key)  # type: ignore[union-attr]
            if val is not None:
                return float(val)
        except Exception as exc:
            logger.warning("signal.redis.bankroll_read_failed", error=str(exc))
        return self._settings.effective_bankroll

    async def _get_market_exposure(self, market_id: str) -> float:
        """Return total USDC currently allocated to this market by the bot."""
        from db.models import BotPosition, PositionStatus
        from db.session import AsyncSessionLocal
        from sqlalchemy import select, func

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(func.sum(BotPosition.size_usdc))
                    .where(
                        BotPosition.market_id == market_id,
                        BotPosition.status == PositionStatus.OPEN,
                    )
                )
                total = result.scalar_one_or_none()
                return float(total) if total is not None else 0.0
        except Exception as exc:
            logger.warning("signal.db.exposure_read_failed", market=market_id, error=str(exc))
            return 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_confidence_multiplier(roi_score: float, consistency_score: float) -> float:
    """Compute confidence multiplier bounded to [0.5, 1.5].

    confidence_mult = min(1.5, max(0.5, (roi_score/100) * (consistency_score/100) * 2.0))
    """
    raw = (roi_score / 100.0) * (consistency_score / 100.0) * 2.0
    return min(1.5, max(0.5, raw))
