"""Polymarket Gamma API client — public REST API, no authentication required.

Used to discover active alpha markets (politics, elections, finance) whose
token IDs are passed to the CLOB WebSocket market channel subscription.
"""

from __future__ import annotations

import ssl
from typing import Any

import aiohttp
import certifi
import structlog

logger = structlog.get_logger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Tags whose markets are most likely to have information asymmetry
ALPHA_TAGS = {
    "politics", "elections", "us-politics", "president",
    "finance", "economics", "macro",
    "crypto", "bitcoin", "ethereum",
    "science", "ai",
}


class GammaMarket:
    """Lightweight market descriptor from the Gamma API."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.condition_id: str = data.get("conditionId") or data.get("condition_id", "")
        self.question: str = data.get("question", "")
        self.category: str = (data.get("category") or "OTHER").upper()
        self.volume: float = float(data.get("volume") or data.get("volumeNum") or 0)
        self.open_interest: float = float(data.get("liquidityNum") or data.get("liquidity") or 0)
        self.active: bool = bool(data.get("active", True))
        # clob_token_ids is a JSON-encoded list of token IDs for YES/NO outcomes
        raw_tokens = data.get("clobTokenIds") or data.get("clob_token_ids") or []
        if isinstance(raw_tokens, str):
            import json
            try:
                raw_tokens = json.loads(raw_tokens)
            except Exception:
                raw_tokens = []
        self.token_ids: list[str] = raw_tokens

    def __repr__(self) -> str:
        return f"GammaMarket({self.condition_id[:12]}… {self.question[:50]})"


class GammaClient:
    """Async client for the public Polymarket Gamma API."""

    def __init__(self) -> None:
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    async def get_alpha_markets(
        self,
        min_volume: float = 10_000.0,
        limit: int = 200,
    ) -> list[GammaMarket]:
        """Return active markets in alpha categories sorted by volume.

        Args:
            min_volume: Minimum total volume to include a market.
            limit: Max markets to return.
        """
        markets: list[GammaMarket] = []

        params = {
            "active": "true",
            "closed": "false",
            "limit": "500",
            "order": "volume",
            "ascending": "false",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{GAMMA_BASE}/markets",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=20.0),
                    ssl=self._ssl_ctx,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("gamma.markets.bad_status", status=resp.status, body=body[:200])
                        return []
                    data = await resp.json(content_type=None)

            raw_markets = data if isinstance(data, list) else data.get("markets", [])
            logger.info("gamma.markets.fetched", count=len(raw_markets))

            for item in raw_markets:
                mkt = GammaMarket(item)
                if not mkt.active:
                    continue
                if mkt.volume < min_volume:
                    continue
                if not mkt.token_ids:
                    continue
                # Filter by alpha tags if available, else accept all high-volume
                tags = [t.get("slug", "").lower() for t in (item.get("tags") or [])]
                category = mkt.category.lower()
                is_alpha = (
                    any(t in ALPHA_TAGS for t in tags)
                    or any(kw in category for kw in ("politic", "election", "financ", "econom", "crypto"))
                    or any(kw in mkt.question.lower() for kw in (
                        "election", "president", "senate", "congress", "fed ", "inflation",
                        "bitcoin", "btc", "interest rate", "trump", "harris", "poll",
                    ))
                )
                if is_alpha:
                    markets.append(mkt)

            # Sort by volume and cap
            markets.sort(key=lambda m: m.volume, reverse=True)
            markets = markets[:limit]

            logger.info(
                "gamma.alpha_markets.selected",
                count=len(markets),
                top=markets[0].question[:60] if markets else "none",
            )
            return markets

        except Exception as exc:
            logger.error("gamma.markets.failed", error=str(exc))
            return []
