"""FastAPI monitoring server — read-only view of bot state.

Reads from the same Postgres DB and Redis instance as the bot.
Run separately: python -m monitor.api
Access at: http://localhost:8080
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import get_settings

logger = structlog.get_logger(__name__)
_settings = get_settings()

app = FastAPI(title="Whale Bot Monitor", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# DB + Redis setup (read-only connections)
# ---------------------------------------------------------------------------

_engine = create_async_engine(_settings.DATABASE_URL, echo=False, pool_size=3, max_overflow=5)
_SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(_engine, expire_on_commit=False)
_redis: aioredis.Redis | None = None  # type: ignore[type-arg]


async def _get_redis() -> aioredis.Redis:  # type: ignore[type-arg]
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(_settings.REDIS_URL, decode_responses=True, socket_timeout=3)
    return _redis


# ---------------------------------------------------------------------------
# Live price + resolution time helpers
# ---------------------------------------------------------------------------

_CLOB_HOST = "https://clob.polymarket.com"


async def _fetch_live_mid_prices(token_ids: list[str]) -> dict[str, float]:
    """Fetch current mid-prices from the public CLOB orderbook (no auth needed)."""
    if not token_ids:
        return {}

    async def _one(session: aiohttp.ClientSession, tid: str) -> tuple[str, float | None]:
        try:
            async with session.get(
                f"{_CLOB_HOST}/book",
                params={"token_id": tid},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status != 200:
                    return tid, None
                data = await resp.json(content_type=None)
                bids = sorted(data.get("bids", []), key=lambda x: -float(x.get("price", 0)))
                asks = sorted(data.get("asks", []), key=lambda x: float(x.get("price", 0)))
                if bids and asks:
                    return tid, round((float(bids[0]["price"]) + float(asks[0]["price"])) / 2, 4)
                if bids:
                    return tid, round(float(bids[0]["price"]), 4)
                if asks:
                    return tid, round(float(asks[0]["price"]), 4)
                return tid, None
        except Exception:
            return tid, None

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[_one(session, tid) for tid in token_ids])
    return {tid: price for tid, price in results if price is not None}


async def _fetch_resolution_times(position_ids: list[int]) -> dict[int, str | None]:
    """Fetch resolution times cached in Redis by PaperTrader when positions were opened."""
    if not position_ids:
        return {}
    try:
        redis = await _get_redis()
        pipe = redis.pipeline()
        for pid in position_ids:
            pipe.get(f"pos:{pid}:resolution_time")
        results = await pipe.execute()
        return dict(zip(position_ids, results))
    except Exception:
        return {}


def _hours_to_resolution(rt_str: str | None) -> float | None:
    if not rt_str:
        return None
    try:
        rt = datetime.fromisoformat(str(rt_str).replace("Z", "+00:00"))
        if rt.tzinfo is None:
            rt = rt.replace(tzinfo=timezone.utc)
        delta = rt - datetime.now(tz=timezone.utc)
        return round(max(0.0, delta.total_seconds() / 3600), 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/api/status")
async def get_status() -> JSONResponse:
    """Bot mode, bankroll, whitelist size."""
    redis = await _get_redis()
    try:
        sim_bankroll = await redis.get("sim:bankroll")
        sim_peak = await redis.get("sim:peak_bankroll")
        live_bankroll = await redis.get("bot:bankroll")
        whitelist_count = await redis.zcard("whale:whitelist")
        circuit_breaker = await redis.get("bot:circuit_breaker_active")
    except Exception:
        sim_bankroll = sim_peak = live_bankroll = None
        whitelist_count = 0
        circuit_breaker = "0"

    mode = "SIMULATION" if _settings.SIMULATION_MODE else "LIVE"
    liquid = float(sim_bankroll) if sim_bankroll else _settings.SIM_BANKROLL_USDC
    peak = float(sim_peak) if sim_peak else liquid

    # Portfolio value = liquid cash + current mark-to-market value of open positions
    # (deployed cost + unrealized pnl). Using liquid-only understates the true value.
    try:
        async with _SessionLocal() as session:
            result = await session.execute(text("""
                SELECT
                    COALESCE(SUM(size_usdc), 0)          AS deployed,
                    COALESCE(SUM(unrealized_pnl_usdc), 0) AS unrealized
                FROM bot_positions
                WHERE status = 'OPEN' AND is_simulated = TRUE
            """))
            r2 = result.mappings().one()
            deployed = float(r2["deployed"])
            unrealized = float(r2["unrealized"])
    except Exception:
        deployed = unrealized = 0.0

    bankroll = liquid + deployed + unrealized   # true portfolio value
    drawdown_pct = ((peak - bankroll) / peak * 100) if peak > 0 else 0.0

    return JSONResponse({
        "mode": mode,
        "liquid_cash": round(liquid, 2),
        "deployed_capital": round(deployed, 2),
        "unrealized_pnl": round(unrealized, 2),
        "bankroll": round(bankroll, 2),          # total portfolio value
        "peak_bankroll": round(peak, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "whitelist_count": whitelist_count or 0,
        "circuit_breaker_active": circuit_breaker == "1",
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
    })


@app.get("/api/metrics")
async def get_metrics() -> JSONResponse:
    """Aggregate P&L metrics from closed simulated positions."""
    async with _SessionLocal() as session:
        rows = await session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'CLOSED')           AS total_closed,
                COUNT(*) FILTER (WHERE status = 'OPEN')             AS total_open,
                COUNT(*) FILTER (WHERE status = 'CLOSED' AND realized_pnl_usdc > 0) AS wins,
                COUNT(*) FILTER (WHERE status = 'CLOSED' AND realized_pnl_usdc <= 0) AS losses,
                COALESCE(SUM(realized_pnl_usdc) FILTER (WHERE status = 'CLOSED'), 0) AS total_realized,
                COALESCE(SUM(unrealized_pnl_usdc) FILTER (WHERE status = 'OPEN'), 0) AS total_unrealized,
                COALESCE(AVG(realized_pnl_usdc) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl,
                COALESCE(MAX(realized_pnl_usdc), 0) AS best_trade,
                COALESCE(MIN(realized_pnl_usdc), 0) AS worst_trade,
                COALESCE(SUM(size_usdc), 0) AS total_deployed
            FROM bot_positions
            WHERE is_simulated = TRUE
        """))
        r = rows.mappings().one()

        total_closed = int(r["total_closed"])
        wins = int(r["wins"])
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0

        # Signal funnel totals
        funnel = await session.execute(text("""
            SELECT
                COUNT(*) AS evaluated,
                COUNT(*) FILTER (WHERE signal_result = 'EXECUTED') AS executed,
                COUNT(*) FILTER (WHERE signal_result = 'SKIPPED')  AS skipped
            FROM signal_events
        """))
        f = funnel.mappings().one()

    return JSONResponse({
        "total_closed": total_closed,
        "total_open": int(r["total_open"]),
        "wins": wins,
        "losses": int(r["losses"]),
        "win_rate": round(win_rate, 1),
        "total_realized_pnl": round(float(r["total_realized"]), 2),
        "total_unrealized_pnl": round(float(r["total_unrealized"]), 2),
        "total_pnl": round(float(r["total_realized"]) + float(r["total_unrealized"]), 2),
        "avg_pnl_per_trade": round(float(r["avg_pnl"]), 2),
        "best_trade": round(float(r["best_trade"]), 2),
        "worst_trade": round(float(r["worst_trade"]), 2),
        "total_deployed_usdc": round(float(r["total_deployed"]), 2),
        "signals_evaluated": int(f["evaluated"]),
        "signals_executed": int(f["executed"]),
        "signals_skipped": int(f["skipped"]),
    })


@app.get("/api/positions")
async def get_positions(limit: int = 50, status: str = "all") -> JSONResponse:
    """Recent positions (open + closed)."""
    async with _SessionLocal() as session:
        where = "WHERE is_simulated = TRUE"
        if status == "open":
            where += " AND status = 'OPEN'"
        elif status == "closed":
            where += " AND status = 'CLOSED'"

        rows = await session.execute(text(f"""
            SELECT
                id, market_question, market_id, score_tier,
                entry_price, current_price, exit_price,
                size_usdc, shares_held,
                realized_pnl_usdc, unrealized_pnl_usdc,
                status, strategy, opened_at, closed_at,
                copied_from_wallet, whale_score_at_entry,
                exit_reason
            FROM bot_positions
            {where}
            ORDER BY opened_at DESC
            LIMIT :limit
        """), {"limit": limit})
        positions = []
        for r in rows.mappings():
            pnl = float(r["realized_pnl_usdc"] or r["unrealized_pnl_usdc"] or 0)
            entry = float(r["entry_price"] or 0)
            current = float(r["current_price"] or r["exit_price"] or entry)
            roi_pct = ((current - entry) / entry * 100) if entry > 0 else 0.0
            wallet = r["copied_from_wallet"] or ""
            positions.append({
                "id": r["id"],
                "market": r["market_question"] or r["market_id"],
                "tier": r["score_tier"] or "?",
                "strategy": r["strategy"] or "COPY",
                "entry_price": round(entry, 4),
                "current_price": round(current, 4),
                "size_usdc": round(float(r["size_usdc"] or 0), 2),
                "pnl": round(pnl, 2),
                "roi_pct": round(roi_pct, 1),
                "status": r["status"],
                "exit_reason": r["exit_reason"],
                "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
                "whale": f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 10 else wallet,
                "whale_score": round(float(r["whale_score_at_entry"] or 0), 1),
            })
    return JSONResponse(positions)


@app.get("/api/positions/by-strategy")
async def get_positions_by_strategy(limit: int = 50) -> JSONResponse:
    """Positions and summary stats grouped by strategy (COPY, MICRO, NO_FLIP).

    Open positions have their current_price refreshed from the live CLOB orderbook
    on every call. Resolution time is fetched from Redis (cached by PaperTrader).
    """
    async with _SessionLocal() as session:
        # Per-strategy summary
        summary_rows = await session.execute(text("""
            SELECT
                COALESCE(strategy, 'COPY') AS strategy,
                COUNT(*) FILTER (WHERE status = 'OPEN')             AS open_count,
                COUNT(*) FILTER (WHERE status = 'CLOSED')           AS closed_count,
                COUNT(*) FILTER (WHERE status = 'CLOSED' AND realized_pnl_usdc > 0) AS wins,
                COALESCE(SUM(size_usdc) FILTER (WHERE status = 'OPEN'), 0) AS deployed,
                COALESCE(SUM(realized_pnl_usdc) FILTER (WHERE status = 'CLOSED'), 0) AS realized_pnl,
                COALESCE(SUM(unrealized_pnl_usdc) FILTER (WHERE status = 'OPEN'), 0) AS unrealized_pnl
            FROM bot_positions
            WHERE is_simulated = TRUE
            GROUP BY strategy
        """))
        summaries: dict[str, Any] = {}
        for r in summary_rows.mappings():
            strat = r["strategy"]
            closed = int(r["closed_count"])
            wins = int(r["wins"])
            summaries[strat] = {
                "open_count": int(r["open_count"]),
                "closed_count": closed,
                "wins": wins,
                "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0.0,
                "deployed_capital": round(float(r["deployed"]), 2),
                "realized_pnl": round(float(r["realized_pnl"]), 2),
                "unrealized_pnl": round(float(r["unrealized_pnl"]), 2),
                "total_pnl": round(float(r["realized_pnl"]) + float(r["unrealized_pnl"]), 2),
                "positions": [],
            }

        # Fetch positions including token_id and shares_held for live price calc
        pos_rows = await session.execute(text("""
            SELECT
                id, market_question, market_id, token_id, score_tier,
                entry_price, current_price, exit_price,
                size_usdc, shares_held, realized_pnl_usdc, unrealized_pnl_usdc,
                status, strategy, opened_at, closed_at,
                copied_from_wallet, whale_score_at_entry, exit_reason
            FROM bot_positions
            WHERE is_simulated = TRUE
            ORDER BY opened_at DESC
            LIMIT :limit
        """), {"limit": limit})
        raw_rows = [dict(r) for r in pos_rows.mappings()]

    # Collect open position token_ids and ids for live enrichment
    open_entries = [
        (r["id"], r["token_id"])
        for r in raw_rows
        if r["status"] == "OPEN" and r["token_id"]
    ]
    open_pos_ids = [e[0] for e in open_entries]
    open_token_ids = list({e[1] for e in open_entries})

    # Fetch live prices + resolution times concurrently
    live_prices, resolution_times = await asyncio.gather(
        _fetch_live_mid_prices(open_token_ids),
        _fetch_resolution_times(open_pos_ids),
    )

    # Build final position rows
    for r in raw_rows:
        strat = r["strategy"] or "COPY"
        entry = float(r["entry_price"] or 0)
        status = r["status"]
        token_id = r["token_id"] or ""
        wallet = r["copied_from_wallet"] or ""
        pos_id = r["id"]

        # Use live CLOB price for open positions; fall back to DB value
        if status == "OPEN" and token_id in live_prices:
            current = live_prices[token_id]
            shares = float(r["shares_held"] or 0)
            pnl = round((current - entry) * shares, 2)
        else:
            current = float(r["current_price"] or r["exit_price"] or entry)
            pnl = round(float(r["realized_pnl_usdc"] or r["unrealized_pnl_usdc"] or 0), 2)

        roi_pct = round((current - entry) / entry * 100, 1) if entry > 0 else 0.0

        row = {
            "id": pos_id,
            "market": r["market_question"] or r["market_id"],
            "tier": r["score_tier"] or "?",
            "entry_price": round(entry, 4),
            "current_price": round(current, 4),
            "size_usdc": round(float(r["size_usdc"] or 0), 2),
            "pnl": pnl,
            "roi_pct": roi_pct,
            "status": status,
            "exit_reason": r["exit_reason"],
            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
            "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
            "whale": f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 10 else (wallet or "—"),
            "whale_score": round(float(r["whale_score_at_entry"] or 0), 1),
            "hours_to_resolution": (
                _hours_to_resolution(resolution_times.get(pos_id))
                if status == "OPEN" else None
            ),
        }
        if strat not in summaries:
            summaries[strat] = {
                "open_count": 0, "closed_count": 0, "wins": 0, "win_rate": 0.0,
                "deployed_capital": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "total_pnl": 0.0, "positions": [],
            }
        summaries[strat]["positions"].append(row)

    return JSONResponse(summaries)


@app.get("/api/snapshots")
async def get_snapshots(days: int = 30) -> JSONResponse:
    """Daily sim snapshots for P&L chart."""
    async with _SessionLocal() as session:
        rows = await session.execute(text("""
            SELECT date, virtual_bankroll, realized_pnl, unrealized_pnl, total_pnl,
                   open_positions, closed_positions, win_count, loss_count, win_rate,
                   signals_evaluated, signals_executed
            FROM sim_daily_snapshots
            ORDER BY date DESC
            LIMIT :days
        """), {"days": days})
        snaps = [dict(r) for r in rows.mappings()]
        snaps.reverse()  # chronological
    return JSONResponse(snaps)


@app.get("/api/signals/funnel")
async def get_signal_funnel() -> JSONResponse:
    """Gate-level breakdown of signal rejections."""
    async with _SessionLocal() as session:
        rows = await session.execute(text("""
            SELECT
                COALESCE(gate_failed, 'EXECUTED') AS gate,
                COUNT(*) AS count
            FROM signal_events
            GROUP BY gate_failed
            ORDER BY count DESC
            LIMIT 20
        """))
        gates = [{"gate": r["gate"], "count": int(r["count"])} for r in rows.mappings()]
    return JSONResponse(gates)


@app.get("/api/whitelist")
async def get_whitelist(limit: int = 20) -> JSONResponse:
    """Top whale wallets by score from Redis sorted set."""
    redis = await _get_redis()
    try:
        pairs = await redis.zrevrange("whale:whitelist", 0, limit - 1, withscores=True)
        wallets = [
            {"wallet": f"{w[:6]}…{w[-4:]}", "full": w, "score": round(s, 1)}
            for w, s in pairs
        ]
    except Exception:
        wallets = []

    # Enrich with P&L contribution from DB
    async with _SessionLocal() as session:
        if wallets:
            addrs = [w["full"] for w in wallets]
            rows = await session.execute(text("""
                SELECT copied_from_wallet,
                       COUNT(*) AS trades,
                       COALESCE(SUM(realized_pnl_usdc), 0) AS total_pnl,
                       COUNT(*) FILTER (WHERE realized_pnl_usdc > 0) AS wins
                FROM bot_positions
                WHERE is_simulated = TRUE AND copied_from_wallet = ANY(:addrs)
                GROUP BY copied_from_wallet
            """), {"addrs": addrs})
            pnl_map: dict[str, Any] = {r["copied_from_wallet"]: r for r in rows.mappings()}
            for w in wallets:
                stats = pnl_map.get(w["full"])
                w["trades"] = int(stats["trades"]) if stats else 0
                w["total_pnl"] = round(float(stats["total_pnl"]), 2) if stats else 0.0
                w["wins"] = int(stats["wins"]) if stats else 0

    return JSONResponse(wallets)


@app.get("/api/latency")
async def get_latency() -> JSONResponse:
    """Signal latency stats — time between whale trade and bot evaluation."""
    redis = await _get_redis()
    try:
        raw = await redis.lrange("bot:latency:samples", 0, -1)
        samples = [float(v) for v in raw if v]
    except Exception:
        samples = []

    if not samples:
        return JSONResponse({"p50_ms": None, "p95_ms": None, "p99_ms": None, "samples": 0, "warning": "no data yet"})

    samples.sort()
    n = len(samples)
    p50 = samples[int(n * 0.50)]
    p95 = samples[min(int(n * 0.95), n - 1)]
    p99 = samples[min(int(n * 0.99), n - 1)]

    warning = None
    if p50 > 30_000:
        warning = "Median latency >30s — signal feed may be polling rather than streaming"
    elif p50 > 5_000:
        warning = "Median latency >5s — consider optimising WebSocket processing pipeline"

    return JSONResponse({
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1),
        "samples": n,
        "warning": warning,
    })


@app.get("/api/tier_breakdown")
async def get_tier_breakdown() -> JSONResponse:
    """P&L broken down by whale score tier."""
    async with _SessionLocal() as session:
        rows = await session.execute(text("""
            SELECT
                COALESCE(score_tier, 'unknown') AS tier,
                COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
                COUNT(*) FILTER (WHERE realized_pnl_usdc > 0) AS wins,
                COALESCE(SUM(realized_pnl_usdc), 0) AS total_pnl,
                COALESCE(AVG(realized_pnl_usdc) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl
            FROM bot_positions
            WHERE is_simulated = TRUE
            GROUP BY score_tier
            ORDER BY total_pnl DESC
        """))
        tiers = []
        for r in rows.mappings():
            closed = int(r["closed"])
            wins = int(r["wins"])
            tiers.append({
                "tier": r["tier"],
                "closed": closed,
                "wins": wins,
                "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0.0,
                "total_pnl": round(float(r["total_pnl"]), 2),
                "avg_pnl": round(float(r["avg_pnl"]), 2),
            })
    return JSONResponse(tiers)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("MONITOR_PORT", "8080"))
    uvicorn.run("monitor.api:app", host="0.0.0.0", port=port, reload=False, log_level="warning")
