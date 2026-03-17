"""CLOB WebSocket subscriber for live trade events.

Two streaming modes:

  stream_market_trades(token_ids, ...)  — PUBLIC, no auth required.
    Subscribes to the CLOB market channel for specific token IDs.
    Fires for every trade in those markets regardless of who placed it.
    Used in simulation mode watching alpha markets (politics, elections, finance).

  stream_trades(wallet_addresses, ...)  — AUTHENTICATED, requires API keys.
    Subscribes to the CLOB user channel for specific whale wallet addresses.
    Only fires for trades made by those specific wallets.
    Used in live mode after whale wallets are discovered.

Both use auto-reconnect with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

import aiohttp
import certifi
import structlog

from config.settings import get_settings
from data.clob_client import TradeEvent

logger = structlog.get_logger(__name__)

_settings = get_settings()

# Type alias for the async trade callback
TradeCallback = Callable[[TradeEvent], Coroutine[Any, Any, None]]


async def stream_trades(
    wallet_addresses: list[str],
    on_trade: TradeCallback,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Connect to the CLOB WebSocket and stream trade events.

    Args:
        wallet_addresses: List of whale wallet addresses to subscribe to.
        on_trade: Async callback invoked for each incoming TradeEvent.
        shutdown_event: Optional event; when set, the stream will exit cleanly.
    """
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    ws_url = _settings.CLOB_WS_URL
    backoff = 1.0
    max_backoff = 120.0

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    while not shutdown_event.is_set():
        try:
            logger.info("ws.connecting", url=ws_url, wallet_count=len(wallet_addresses))
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    ws_url,
                    heartbeat=30.0,
                    timeout=aiohttp.ClientWSTimeout(ws_close=30.0),
                    receive_timeout=60.0,
                    ssl=ssl_ctx,
                ) as ws:
                    logger.info("ws.connected")
                    backoff = 1.0  # reset on successful connect

                    # Subscribe to user channel for each wallet
                    await _subscribe(ws, wallet_addresses)

                    # Main message loop
                    async for msg in ws:
                        if shutdown_event.is_set():
                            break

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await _handle_message(msg.data, wallet_addresses, on_trade)

                        elif msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong()
                            logger.debug("ws.pong_sent")

                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("ws.server_closed", code=ws.close_code)
                            break

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error("ws.error", error=str(ws.exception()))
                            break

        except asyncio.CancelledError:
            logger.info("ws.cancelled")
            break

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("ws.connection_error", error=str(exc), reconnect_in=backoff)

        if not shutdown_event.is_set():
            logger.info("ws.reconnecting", wait_seconds=backoff)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
                # If we get here the event was set — exit cleanly
                break
            except asyncio.TimeoutError:
                pass  # Normal — sleep elapsed, loop again
            backoff = min(backoff * 2, max_backoff)

    logger.info("ws.stream_stopped")


# ---------------------------------------------------------------------------
# Market-channel streaming (PUBLIC — no auth required)
# ---------------------------------------------------------------------------

# Base WS URL without the /user suffix
_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def stream_market_trades(
    token_ids: list[str],
    on_trade: TradeCallback,
    shutdown_event: asyncio.Event | None = None,
    min_size_usdc: float = 100.0,
) -> None:
    """Stream all trades in specific markets via the public CLOB market channel.

    No authentication required. Fires for every trade in the subscribed markets
    regardless of which wallet executed it.

    Args:
        token_ids: CLOB token IDs to subscribe to (YES/NO tokens for each market).
        on_trade: Async callback invoked for each qualifying TradeEvent.
        shutdown_event: Optional shutdown signal.
        min_size_usdc: Minimum trade size to forward (filters noise).
    """
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    if not token_ids:
        logger.warning("ws.market.no_tokens — nothing to subscribe to")
        return

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    backoff = 1.0
    max_backoff = 120.0

    while not shutdown_event.is_set():
        try:
            logger.info("ws.market.connecting", token_count=len(token_ids))
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    _MARKET_WS_URL,
                    heartbeat=30.0,
                    timeout=aiohttp.ClientWSTimeout(ws_close=30.0),
                    receive_timeout=60.0,
                    ssl=ssl_ctx,
                ) as ws:
                    logger.info("ws.market.connected")
                    backoff = 1.0

                    # Subscribe in batches of 50 tokens
                    await _subscribe_market(ws, token_ids)

                    async for msg in ws:
                        if shutdown_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await _handle_market_message(
                                msg.data, token_ids, on_trade, min_size_usdc
                            )
                        elif msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong()
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning(
                                "ws.market.server_closed",
                                close_code=ws.close_code,
                                close_message=str(msg.data)[:200],
                            )
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning(
                                "ws.market.error",
                                error=str(ws.exception()),
                            )
                            break

        except asyncio.CancelledError:
            break
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "ws.market.connection_error",
                exc_type=type(exc).__name__,
                error=str(exc) or "(no message)",
                reconnect_in=backoff,
            )

        if not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, max_backoff)

    logger.info("ws.market.stream_stopped")


async def _subscribe_market(
    ws: aiohttp.ClientWebSocketResponse,
    token_ids: list[str],
) -> None:
    """Subscribe to the market channel for a list of token IDs.

    Polymarket's market channel distinguishes between:
      - ``assets_ids``: list of CLOB token IDs (YES/NO outcome tokens)
      - ``markets``:    list of condition IDs (hex market identifiers)

    We pass token IDs so we use ``assets_ids``.
    """
    # Polymarket market channel accepts up to 50 assets per message
    batch_size = 50
    for i in range(0, len(token_ids), batch_size):
        batch = token_ids[i: i + batch_size]
        sub_msg = json.dumps({
            "type": "subscribe",
            "channel": "market",
            "assets_ids": batch,   # token IDs → assets_ids, NOT markets
        })
        await ws.send_str(sub_msg)
        logger.debug("ws.market.subscribed_batch", count=len(batch))
        await asyncio.sleep(0.05)


async def _handle_market_message(
    raw: str,
    token_ids: list[str],
    on_trade: TradeCallback,
    min_size_usdc: float,
) -> None:
    """Parse market-channel messages and dispatch trade events for large trades.

    Polymarket market channel sends two message shapes:

    1. Initial book snapshot (on subscribe):
       {"market": "<condition_id>", "asset_id": "<token_id>",
        "bids": [...], "asks": [...], "timestamp": "..."}

    2. Trade/price-change event (on each fill):
       {"market": "<condition_id>",
        "price_changes": [{"asset_id": "<token_id>", "price": "0.65",
                           "size": "1200", "side": "BUY",
                           "hash": "0x...", "best_bid": "...",
                           "best_ask": "...", "timestamp": "..."}]}

    We ignore book snapshots and emit a TradeEvent for every price_change
    fill whose USDC value is >= min_size_usdc.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    # Messages arrive as either a single object or a list
    messages = data if isinstance(data, list) else [data]
    token_set = set(token_ids)

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        market_id = msg.get("market", "")

        # price_changes: list of individual fills
        price_changes = msg.get("price_changes")
        if not price_changes:
            # Book snapshot or heartbeat — skip
            continue

        for change in price_changes:
            if not isinstance(change, dict):
                continue

            token_id = change.get("asset_id", "")
            if token_id and token_id not in token_set:
                continue

            try:
                price = float(change.get("price", 0) or 0)
                size = float(change.get("size", 0) or 0)
                # size is in shares (outcome tokens); multiply by price to get USDC
                size_usdc = size * price if price > 0 else size

                if size_usdc < min_size_usdc:
                    continue

                ts_raw = change.get("timestamp") or time.time()
                if isinstance(ts_raw, str) and ts_raw.isdigit():
                    # Milliseconds timestamp string
                    timestamp = datetime.utcfromtimestamp(int(ts_raw) / 1000)
                elif isinstance(ts_raw, (int, float)):
                    ts_f = float(ts_raw)
                    # Heuristic: ms timestamps are > 1e12
                    timestamp = datetime.utcfromtimestamp(ts_f / 1000 if ts_f > 1e12 else ts_f)
                else:
                    timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))

                side_raw = (change.get("side") or "BUY").upper()
                side = "BUY" if side_raw in ("BUY", "LONG", "YES") else "SELL"

                trade = TradeEvent(
                    wallet_address="MARKET_TRADE",  # no wallet on public channel
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    price=price,
                    size_usdc=size_usdc,
                    timestamp=timestamp,
                    transaction_hash=change.get("hash", ""),
                )

                logger.info(
                    "ws.market.large_trade",
                    market=market_id[:16],
                    token=token_id[:16],
                    price=price,
                    size_usdc=round(size_usdc, 2),
                    side=side,
                )

                await on_trade(trade)

            except Exception as exc:
                logger.warning("ws.market.parse_error", error=str(exc), change=str(change)[:200])


async def _subscribe(ws: aiohttp.ClientWebSocketResponse, wallet_addresses: list[str]) -> None:
    """Send subscription messages for the user channel."""
    for wallet in wallet_addresses:
        sub_msg = json.dumps({
            "auth": {
                "apiKey": _settings.POLYMARKET_API_KEY,
                "secret": _settings.POLYMARKET_API_SECRET,
                "passphrase": _settings.POLYMARKET_API_PASSPHRASE,
            },
            "type": "subscribe",
            "channel": "user",
            "markets": [],
            "user": wallet,
        })
        await ws.send_str(sub_msg)
        logger.debug("ws.subscribed", wallet=wallet)

    # Small delay to avoid flooding the server
    await asyncio.sleep(0.1)


async def _handle_message(
    raw: str,
    wallet_addresses: list[str],
    on_trade: TradeCallback,
) -> None:
    """Parse an incoming WebSocket message and dispatch trade events."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ws.message.invalid_json", raw=raw[:200])
        return

    # The CLOB WS sends lists of events
    if isinstance(data, list):
        events = data
    elif isinstance(data, dict):
        events = data.get("data", []) if isinstance(data.get("data"), list) else [data]
    else:
        return

    wallet_set = {w.lower() for w in wallet_addresses}

    for event in events:
        if not isinstance(event, dict):
            continue

        event_type = event.get("event_type", "").lower()
        if event_type not in ("trade", "order_filled", "fill"):
            continue

        try:
            trade = _parse_ws_trade(event)
        except Exception as exc:
            logger.warning("ws.trade.parse_error", error=str(exc), event=str(event)[:200])
            continue

        if trade.wallet_address.lower() not in wallet_set:
            continue

        logger.debug(
            "ws.trade.received",
            wallet=trade.wallet_address,
            market=trade.market_id,
            side=trade.side,
            size_usdc=trade.size_usdc,
        )

        try:
            await on_trade(trade)
        except Exception as exc:
            logger.error("ws.on_trade.callback_error", error=str(exc))


def _parse_ws_trade(event: dict[str, Any]) -> TradeEvent:
    """Convert a raw WebSocket trade event to a TradeEvent model."""
    ts_raw = event.get("timestamp") or event.get("created_at") or time.time()
    if isinstance(ts_raw, (int, float)):
        timestamp = datetime.utcfromtimestamp(float(ts_raw))
    else:
        timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))

    maker_address = event.get("maker_address", event.get("user", ""))
    taker_address = event.get("taker_address", "")
    # Prefer maker address as the "whale" — taker is the passive side
    wallet_address = maker_address or taker_address

    side_raw = event.get("side", event.get("maker_side", "BUY")).upper()
    side = "BUY" if side_raw in ("BUY", "LONG") else "SELL"

    return TradeEvent(
        wallet_address=wallet_address,
        market_id=event.get("market", event.get("condition_id", "")),
        token_id=event.get("asset_id", event.get("token_id", "")),
        side=side,
        price=float(event.get("price", 0)),
        size_usdc=float(event.get("size", 0)),
        timestamp=timestamp,
        transaction_hash=event.get("transaction_hash", event.get("tx_hash", "")),
    )
