"""FastAPI monitoring server — read-only view of bot state.

Reads from the same Postgres DB and Redis instance as the bot.
Run separately: python -m monitor.api
Access at: http://localhost:8080
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
                status, opened_at, closed_at,
                copied_from_wallet, whale_score_at_entry
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
                "entry_price": round(entry, 4),
                "current_price": round(current, 4),
                "size_usdc": round(float(r["size_usdc"] or 0), 2),
                "pnl": round(pnl, 2),
                "roi_pct": round(roi_pct, 1),
                "status": r["status"],
                "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
                "whale": f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 10 else wallet,
                "whale_score": round(float(r["whale_score_at_entry"] or 0), 1),
            })
    return JSONResponse(positions)


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
