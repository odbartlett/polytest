#!/usr/bin/env python3
"""Reset simulation data — wipe all positions, signal events, and bankroll.

Run this before starting a new simulation run to get a clean slate.

Usage (local):
    python scripts/reset_sim.py

Usage (against Railway — export the env vars first):
    export DATABASE_URL="postgresql://..."   # from Railway dashboard
    export REDIS_URL="redis://..."           # from Railway dashboard
    python scripts/reset_sim.py
"""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> None:
    # Inline imports so we can report errors clearly
    try:
        import asyncpg
        import redis.asyncio as aioredis
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Run: pip install asyncpg redis")
        sys.exit(1)

    db_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://botuser:botpassword@localhost:5432/polymarket_bot")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Normalise to plain asyncpg URL (strip dialect prefix if present)
    pg_url = db_url
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if pg_url.startswith(prefix):
            pg_url = "postgresql://" + pg_url[len(prefix):]
    if pg_url.startswith("postgresql+"):
        pg_url = "postgresql://" + pg_url.split("://", 1)[1]

    print(f"Connecting to database...")
    try:
        conn = await asyncpg.connect(pg_url)
    except Exception as e:
        print(f"Database connection failed: {e}")
        print(f"URL used: {pg_url[:40]}...")
        sys.exit(1)

    print("Clearing simulation data...")
    tables_cleared = {}
    async with conn.transaction():
        for table in ("bot_orders", "bot_positions", "signal_events", "sim_daily_snapshots", "trades"):
            try:
                result = await conn.execute(f"DELETE FROM {table} WHERE TRUE")
                # result is like "DELETE N"
                count = int(result.split()[-1])
                tables_cleared[table] = count
            except Exception as e:
                tables_cleared[table] = f"ERROR: {e}"

    await conn.close()

    print("\nDatabase cleared:")
    for table, count in tables_cleared.items():
        print(f"  {table}: {count} rows deleted")

    print(f"\nConnecting to Redis ({redis_url.split('@')[-1]})...")
    try:
        r = aioredis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        await r.ping()

        keys_deleted = []
        for key in ("sim:bankroll", "sim:peak_bankroll", "bot:circuit_breaker_active",
                    "bot:daily_loss", "bot:bankroll"):
            deleted = await r.delete(key)
            if deleted:
                keys_deleted.append(key)

        await r.aclose()

        if keys_deleted:
            print(f"\nRedis keys cleared: {', '.join(keys_deleted)}")
        else:
            print("\nNo Redis sim keys found (already clean).")
    except Exception as e:
        print(f"\nRedis error (non-fatal): {e}")

    print("\nDone. Restart the bot to begin a fresh simulation run.")


if __name__ == "__main__":
    asyncio.run(main())
