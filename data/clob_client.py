"""Polymarket CLOB REST API async client.

Authentication uses HMAC-SHA256 per Polymarket's L1 auth spec.
Order signing uses py-clob-client (CPU-bound, called synchronously).
All I/O is performed via aiohttp with exponential-backoff retry.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import ssl
import time
from datetime import datetime
from typing import Any, Optional

import aiohttp
import certifi
import structlog
from pydantic import BaseModel, Field, field_validator

from config.settings import get_settings

logger = structlog.get_logger(__name__)

_settings = get_settings()

# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class Token(BaseModel):
    token_id: str
    outcome: str  # "YES" / "NO"


class Market(BaseModel):
    market_id: str = Field(alias="condition_id")
    question: str
    category: str = "OTHER"
    open_interest: float = 0.0
    resolution_time: Optional[datetime] = None
    active: bool = True
    tokens: list[Token] = Field(default_factory=list)
    market_slug: str = ""

    model_config = {"populate_by_name": True}

    @field_validator("category", mode="before")
    @classmethod
    def normalise_category(cls, v: Any) -> str:
        mapping = {
            "politics": "POLITICS",
            "crypto": "CRYPTO",
            "sports": "SPORTS",
            "economics": "ECONOMICS",
            "science": "SCIENCE_GEO",
            "geo": "SCIENCE_GEO",
            "geography": "SCIENCE_GEO",
        }
        raw = str(v).lower()
        for key, normalised in mapping.items():
            if key in raw:
                return normalised
        return "OTHER"


class OrderLevel(BaseModel):
    price: float
    size: float


class Orderbook(BaseModel):
    token_id: str
    bids: list[OrderLevel] = Field(default_factory=list)
    asks: list[OrderLevel] = Field(default_factory=list)

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        if self.bids:
            return self.bids[0].price
        if self.asks:
            return self.asks[0].price
        return 0.5

    def depth_within_slippage(self, side: str, max_slippage: float) -> float:
        """Return total USDC depth available within max_slippage of best price."""
        if side == "BUY":
            levels = self.asks
            if not levels:
                return 0.0
            best = levels[0].price
            threshold = best * (1 + max_slippage)
            return sum(lv.price * lv.size for lv in levels if lv.price <= threshold)
        else:
            levels = self.bids
            if not levels:
                return 0.0
            best = levels[0].price
            threshold = best * (1 - max_slippage)
            return sum(lv.price * lv.size for lv in levels if lv.price >= threshold)


class TradeEvent(BaseModel):
    wallet_address: str
    market_id: str
    token_id: str
    side: str  # "BUY" / "SELL"
    price: float
    size_usdc: float
    timestamp: datetime
    transaction_hash: str = ""


class Position(BaseModel):
    wallet_address: str
    market_id: str
    token_id: str
    size: float
    avg_price: float
    side: str


class OrderResult(BaseModel):
    order_id: str
    status: str
    market_id: str
    token_id: str
    side: str
    price: float
    size: float


class OrderStatus(BaseModel):
    order_id: str
    status: str  # "PENDING" / "FILLED" / "CANCELLED" / "EXPIRED"
    fill_price: Optional[float] = None
    filled_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _retry_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    timeout: float = 10.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute an HTTP request with exponential-backoff retry on 429/5xx."""
    backoff = 1.0
    last_exc: Exception = RuntimeError("No attempts made")

    for attempt in range(max_retries + 1):
        try:
            async with session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs
            ) as resp:
                if resp.status == 429 or resp.status >= 500:
                    text = await resp.text()
                    logger.warning(
                        "clob.request.retryable_error",
                        attempt=attempt,
                        status=resp.status,
                        url=url,
                        body=text[:200],
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    resp.raise_for_status()

                if resp.status == 204:
                    return {}

                data: Any = await resp.json(content_type=None)
                return data if isinstance(data, dict) else {"results": data}

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "clob.request.network_error",
                attempt=attempt,
                url=url,
                error=str(exc),
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    raise last_exc


# ---------------------------------------------------------------------------
# CLOBClient
# ---------------------------------------------------------------------------


class CLOBClient:
    """Async Polymarket CLOB REST client with HMAC authentication."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._session: Optional[aiohttp.ClientSession] = None
        self._base_url = self._settings.CLOB_BASE_URL.rstrip("/")
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    async def __aenter__(self) -> "CLOBClient":
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        self._session = aiohttp.ClientSession(
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            connector=connector,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _make_auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Generate HMAC-SHA256 authentication headers for the CLOB API.

        Returns an empty dict when API credentials are not configured (sim mode
        with public endpoints like GET /markets and GET /book).
        """
        if not self._settings.POLYMARKET_API_SECRET or not self._settings.POLYMARKET_API_KEY:
            return {}
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        raw_sig = hmac.new(
            self._settings.POLYMARKET_API_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(raw_sig).decode("utf-8")
        return {
            "POLY-API-KEY": self._settings.POLYMARKET_API_KEY,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self._settings.POLYMARKET_API_PASSPHRASE,
        }

    def _session_or_raise(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("CLOBClient must be used as async context manager")
        return self._session

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        session = self._session_or_raise()
        url = f"{self._base_url}{path}"
        headers = self._make_auth_headers("GET", path)
        return await _retry_request(session, "GET", url, headers=headers, params=params)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._session_or_raise()
        url = f"{self._base_url}{path}"
        body = json.dumps(payload)
        headers = self._make_auth_headers("POST", path, body)
        return await _retry_request(session, "POST", url, headers=headers, data=body)

    async def _delete(self, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        session = self._session_or_raise()
        url = f"{self._base_url}{path}"
        body = json.dumps(payload) if payload else ""
        headers = self._make_auth_headers("DELETE", path, body)
        return await _retry_request(
            session, "DELETE", url, headers=headers, data=body if body else None
        )

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_markets(self, active_only: bool = True) -> list[Market]:
        """Fetch all markets, optionally filtering to active ones."""
        results: list[Market] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["next_cursor"] = cursor
            data = await self._get("/markets", params=params)
            raw_markets = data.get("data", [])
            for raw in raw_markets:
                try:
                    market = _parse_market(raw)
                    if active_only and not market.active:
                        continue
                    results.append(market)
                except Exception as exc:
                    logger.warning("clob.market.parse_error", error=str(exc), raw=str(raw)[:200])
            cursor = data.get("next_cursor", "")
            if not cursor or cursor == "LTE=":
                break
        logger.info("clob.markets.fetched", count=len(results))
        return results

    async def get_market(self, market_id: str) -> Market:
        """Fetch a single market by condition ID."""
        data = await self._get(f"/markets/{market_id}")
        return _parse_market(data)

    async def get_orderbook(self, token_id: str) -> Orderbook:
        """Fetch the full orderbook for a token."""
        data = await self._get("/book", params={"token_id": token_id})
        return _parse_orderbook(token_id, data)

    async def get_trades(
        self,
        wallet_address: str,
        start_ts: int,
        end_ts: int,
        limit: int = 500,
    ) -> list[TradeEvent]:
        """Fetch trades for a wallet address within a timestamp range."""
        data = await self._get(
            "/trades",
            params={
                "maker_address": wallet_address,
                "startTs": start_ts,
                "endTs": end_ts,
                "limit": limit,
            },
        )
        trades: list[TradeEvent] = []
        for raw in data.get("data", []):
            try:
                trades.append(_parse_trade_event(raw, wallet_address))
            except Exception as exc:
                logger.warning("clob.trade.parse_error", error=str(exc))
        return trades

    async def get_positions(self, wallet_address: str) -> list[Position]:
        """Fetch open positions for a wallet."""
        data = await self._get(f"/positions/{wallet_address}")
        positions: list[Position] = []
        for raw in data.get("data", []):
            try:
                positions.append(
                    Position(
                        wallet_address=wallet_address,
                        market_id=raw.get("market", ""),
                        token_id=raw.get("asset_id", ""),
                        size=float(raw.get("size", 0)),
                        avg_price=float(raw.get("avg_price", 0)),
                        side=raw.get("side", "BUY"),
                    )
                )
            except Exception as exc:
                logger.warning("clob.position.parse_error", error=str(exc))
        return positions

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> OrderResult:
        """Sign and place a limit order via the CLOB API.

        Uses py-clob-client for EIP-712 order signing (CPU-bound).
        """
        import asyncio
        from functools import partial

        loop = asyncio.get_event_loop()
        signed_order = await loop.run_in_executor(
            None,
            partial(_sign_order, token_id=token_id, side=side, price=price, size=size),
        )
        data = await self._post("/order", signed_order)
        order_id = data.get("orderID") or data.get("order_id", "")
        return OrderResult(
            order_id=order_id,
            status=data.get("status", "PENDING"),
            market_id=data.get("market", ""),
            token_id=token_id,
            side=side,
            price=price,
            size=size,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        try:
            await self._delete(f"/order/{order_id}")
            logger.info("clob.order.cancelled", order_id=order_id)
            return True
        except Exception as exc:
            logger.error("clob.order.cancel_failed", order_id=order_id, error=str(exc))
            return False

    async def get_order_status(self, order_id: str) -> OrderStatus:
        """Poll the status of a single order."""
        data = await self._get(f"/order/{order_id}")
        status_map = {
            "matched": "FILLED",
            "filled": "FILLED",
            "cancelled": "CANCELLED",
            "canceled": "CANCELLED",
            "expired": "EXPIRED",
            "open": "PENDING",
            "live": "PENDING",
        }
        raw_status = str(data.get("status", "open")).lower()
        normalised = status_map.get(raw_status, "PENDING")
        fill_price: Optional[float] = None
        filled_at: Optional[datetime] = None
        if data.get("matched_price"):
            fill_price = float(data["matched_price"])
        if data.get("updated_at"):
            try:
                filled_at = datetime.fromisoformat(str(data["updated_at"]).replace("Z", "+00:00"))
            except ValueError:
                pass
        return OrderStatus(
            order_id=order_id,
            status=normalised,
            fill_price=fill_price,
            filled_at=filled_at,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_market(raw: dict[str, Any]) -> Market:
    tokens_raw = raw.get("tokens", [])
    tokens = [
        Token(token_id=t.get("token_id", ""), outcome=t.get("outcome", ""))
        for t in tokens_raw
    ]
    resolution_time: Optional[datetime] = None
    if raw.get("end_date_iso"):
        try:
            resolution_time = datetime.fromisoformat(
                str(raw["end_date_iso"]).replace("Z", "+00:00")
            )
        except ValueError:
            pass

    return Market(
        condition_id=raw.get("condition_id", raw.get("market_id", "")),
        question=raw.get("question", ""),
        category=raw.get("category", "OTHER"),
        open_interest=float(raw.get("volume", 0) or 0),
        resolution_time=resolution_time,
        active=bool(raw.get("active", True)),
        tokens=tokens,
        market_slug=raw.get("market_slug", ""),
    )


def _parse_orderbook(token_id: str, raw: dict[str, Any]) -> Orderbook:
    def parse_levels(levels: list[Any]) -> list[OrderLevel]:
        result: list[OrderLevel] = []
        for lv in levels:
            if isinstance(lv, dict):
                result.append(OrderLevel(price=float(lv.get("price", 0)), size=float(lv.get("size", 0))))
            elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
                result.append(OrderLevel(price=float(lv[0]), size=float(lv[1])))
        return result

    return Orderbook(
        token_id=token_id,
        bids=parse_levels(raw.get("bids", [])),
        asks=parse_levels(raw.get("asks", [])),
    )


def _parse_trade_event(raw: dict[str, Any], wallet_address: str) -> TradeEvent:
    ts_raw = raw.get("timestamp") or raw.get("created_at") or ""
    if isinstance(ts_raw, (int, float)):
        timestamp = datetime.utcfromtimestamp(float(ts_raw))
    else:
        timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))

    side = "BUY" if raw.get("side", "").upper() in ("BUY", "LONG") else "SELL"
    return TradeEvent(
        wallet_address=wallet_address,
        market_id=raw.get("market", ""),
        token_id=raw.get("asset_id", raw.get("token_id", "")),
        side=side,
        price=float(raw.get("price", 0)),
        size_usdc=float(raw.get("size", 0)),
        timestamp=timestamp,
        transaction_hash=raw.get("transaction_hash", ""),
    )


def _sign_order(
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> dict[str, Any]:
    """Synchronously sign a CLOB limit order using py-clob-client.

    This function runs in a thread executor to keep the event loop unblocked.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

    settings = get_settings()
    client = ClobClient(
        host=settings.CLOB_BASE_URL,
        key=settings.POLYMARKET_PRIVATE_KEY,
        chain_id=137,  # Polygon mainnet
        creds=ApiCreds(
            api_key=settings.POLYMARKET_API_KEY,
            api_secret=settings.POLYMARKET_API_SECRET,
            api_passphrase=settings.POLYMARKET_API_PASSPHRASE,
        ),
    )
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
        order_type=OrderType.GTC,
    )
    signed = client.create_order(order_args)
    # Return the dict payload ready for the POST /order endpoint
    return signed if isinstance(signed, dict) else signed.dict()
