"""Seed the whale whitelist from the Polymarket public leaderboard.

Runs standalone — does NOT require the bot to be running.
Populates both Postgres (wallet_scores) and Redis (whale:whitelist).

Usage:
    python scripts/seed_whales.py                  # auto-discover from leaderboard
    python scripts/seed_whales.py 0xABC 0xDEF ...  # seed specific addresses
"""

from __future__ import annotations

import asyncio
import ssl
import sys
from datetime import datetime, timezone

import aiohttp
import certifi
import redis.asyncio as aioredis
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, ".")

from config.settings import get_settings
from db.models import WalletScore

settings = get_settings()
REDIS_WHITELIST_KEY = "whale:whitelist"


async def fetch_leaderboard_wallets() -> list[str]:
    """Fetch top traders from the public Polymarket leaderboard API."""
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    discovered: list[str] = []

    for window in ("monthly", "all"):
        url = "https://data-api.polymarket.com/leaderboard"
        params = {"window": window, "limit": "100", "offset": "0"}
        print(f"Fetching {window} leaderboard from {url} ...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=20.0),
                    ssl=ssl_ctx,
                ) as resp:
                    print(f"  HTTP {resp.status}")
                    if resp.status != 200:
                        text = await resp.text()
                        print(f"  Body: {text[:300]}")
                        continue
                    data = await resp.json(content_type=None)

            entries = data if isinstance(data, list) else data.get("data", data.get("leaderboard", []))
            print(f"  Got {len(entries)} entries")

            # Print first entry to see schema
            if entries:
                print(f"  Sample entry keys: {list(entries[0].keys())}")

            for entry in entries:
                addr = (
                    entry.get("address")
                    or entry.get("user")
                    or entry.get("proxy_wallet")
                    or ""
                )
                if addr and len(addr) == 42 and addr.startswith("0x"):
                    discovered.append(addr)

        except Exception as exc:
            print(f"  Error: {exc}")

    unique = list(dict.fromkeys(discovered))
    print(f"\nTotal unique wallets discovered: {len(unique)}")
    return unique


async def seed(wallet_addresses: list[str]) -> None:
    if not wallet_addresses:
        print("No wallets to seed.")
        return

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    now = datetime.now(tz=timezone.utc)

    # Assign scores descending (first = highest score from leaderboard rank)
    n = len(wallet_addresses)
    scored: list[tuple[str, float]] = []
    for i, addr in enumerate(wallet_addresses):
        # Score from 80 down to 55 based on rank
        score = round(80.0 - (25.0 * i / max(n - 1, 1)), 1)
        score = max(55.0, score)
        scored.append((addr, score))

    print(f"\nSeeding {len(scored)} wallets ...")

    async with SessionLocal() as session:
        async with session.begin():
            for addr, score in scored:
                stmt = pg_insert(WalletScore).values(
                    wallet_address=addr,
                    whale_score=score,
                    roi_score=score * 0.35,
                    consistency_score=score * 0.25,
                    sizing_score=score * 0.20,
                    specialization_score=score * 0.10,
                    recency_score=score * 0.10,
                    total_volume_usdc=settings.MIN_TOTAL_VOLUME_USDC * 2,
                    resolved_markets_count=0,
                    win_count=0,
                    last_scored_at=now,
                ).on_conflict_do_update(
                    index_elements=["wallet_address"],
                    set_={"whale_score": score, "last_scored_at": now},
                )
                await session.execute(stmt)

    print(f"  ✓ Postgres: upserted {len(scored)} rows into wallet_scores")

    mapping = {addr: score for addr, score in scored}
    await redis.zadd(REDIS_WHITELIST_KEY, mapping)
    total = await redis.zcard(REDIS_WHITELIST_KEY)
    print(f"  ✓ Redis: whale:whitelist now has {total} entries")

    print(f"\nTop 10:")
    for addr, score in scored[:10]:
        print(f"  {addr[:10]}…{addr[-6:]}  score={score}")

    await redis.aclose()
    await engine.dispose()
    print("\nDone. Restart the bot to use the new whitelist.")


async def main() -> None:
    manual = [a for a in sys.argv[1:] if a.startswith("0x")]
    if manual:
        print(f"Seeding {len(manual)} manually specified addresses ...")
        await seed(manual)
    else:
        wallets = await fetch_leaderboard_wallets()
        await seed(wallets)


if __name__ == "__main__":
    asyncio.run(main())
