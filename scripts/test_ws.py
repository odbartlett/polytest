"""Quick WebSocket connectivity test — runs standalone, no DB/Redis needed.

Usage:
    python scripts/test_ws.py

Connects to the Polymarket market WebSocket, subscribes to a handful of
real token IDs fetched live from the Gamma API, then prints whatever the
server sends for 30 seconds.  Exits cleanly on Ctrl-C.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import sys

import aiohttp
import certifi

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


async def fetch_token_ids(n: int = 10) -> list[str]:
    """Grab the first *n* token IDs from the top-volume active markets."""
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    params = {"active": "true", "closed": "false", "limit": "20", "order": "volume", "ascending": "false"}
    async with aiohttp.ClientSession() as session:
        async with session.get(GAMMA_URL, params=params, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)

    raw = data if isinstance(data, list) else data.get("markets", [])
    token_ids: list[str] = []
    for item in raw:
        tids = item.get("clobTokenIds") or []
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                tids = []
        token_ids.extend(tids)
        if len(token_ids) >= n:
            break
    return token_ids[:n]


async def main() -> None:
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    print("Fetching token IDs from Gamma API…")
    token_ids = await fetch_token_ids(10)
    if not token_ids:
        print("ERROR: No token IDs returned from Gamma API.")
        sys.exit(1)
    print(f"Got {len(token_ids)} token IDs: {token_ids[:3]}…")

    print(f"\nConnecting to {MARKET_WS_URL}")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            MARKET_WS_URL,
            heartbeat=30.0,
            ssl=ssl_ctx,
            timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
            receive_timeout=35.0,
        ) as ws:
            print("Connected. Sending subscription…")

            sub_msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "assets_ids": token_ids,
            })
            print(f"  Subscription: {sub_msg[:120]}…")
            await ws.send_str(sub_msg)
            print("  Subscription sent. Waiting for messages (30s)…\n")

            deadline = asyncio.get_event_loop().time() + 30
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    print(f"MSG: {msg.data[:300]}")
                elif msg.type == aiohttp.WSMsgType.PING:
                    await ws.pong()
                    print("PING → PONG")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    print(f"SERVER CLOSED: code={ws.close_code} data={msg.data!r}")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"WS ERROR: {ws.exception()}")
                    break

                if asyncio.get_event_loop().time() >= deadline:
                    print("\n30 s elapsed — closing cleanly.")
                    break

    print("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
