#!/usr/bin/env python3
"""Post-session analysis agent.

Fetches performance data from the monitoring API, then uses Claude Opus 4.6
with adaptive thinking to identify what went wrong and how to improve.

Usage:
    python scripts/analyze_results.py
    python scripts/analyze_results.py --url https://your-railway-url.railway.app
    python scripts/analyze_results.py --url http://localhost:8080 --output report.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import ssl

import aiohttp
import anthropic
import certifi


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


async def fetch_all(base_url: str) -> dict:
    """Fetch all monitoring API endpoints and return combined data dict."""
    base_url = base_url.rstrip("/")
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30)) as session:
        async def get(path: str, fallback=None):
            try:
                async with session.get(f"{base_url}{path}") as r:
                    if r.status >= 400:
                        print(f"  Warning: {path} returned {r.status}, skipping.")
                        return fallback if fallback is not None else {}
                    return await r.json()
            except Exception as e:
                print(f"  Warning: {path} failed ({e}), skipping.")
                return fallback if fallback is not None else {}

        status, metrics, positions, funnel, tiers = await asyncio.gather(
            get("/api/status"),
            get("/api/metrics"),
            get("/api/positions?limit=200&status=all", fallback=[]),
            get("/api/signals/funnel", fallback=[]),
            get("/api/tier_breakdown", fallback=[]),
        )

    return {
        "status": status,
        "metrics": metrics,
        "positions": positions,
        "signal_funnel": funnel,
        "tier_breakdown": tiers,
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert quantitative trading system analyst.
You have deep knowledge of prediction markets, copy-trading strategies,
and signal engineering. Your analysis should be concrete and actionable —
specific parameter values, not vague suggestions. Point out both problems
and what is working well. Be honest when sample sizes are too small to
draw strong conclusions."""

GATE_DESCRIPTIONS = """
Signal Gates (evaluated in order, first failure short-circuits):
  1. TRADE_IS_BUY       — Only copy BUY-side trades
  2. TRADE_SIZE_MIN     — Minimum $500 trade size (filters noise)
  3. PRICE_RANGE        — Token price must be 0.05–0.70 (avoids near-resolved markets)
  4. WHALE_SCORE_MIN    — Whale reputation score ≥ 55 (0–100 scale)
  5. MARKET_OI_MIN      — Market open interest ≥ $50,000
  6. ORDERBOOK_DEPTH    — Enough liquidity for the computed copy size
  7. POSITION_CAP       — Already-open exposure in this market < 5% of bankroll
  8. TIME_TO_RESOLUTION — ≥ 6 hours until market resolves
  9. CIRCUIT_BREAKER    — Halt if portfolio drawdown exceeds 15%

Copy sizing formula:
  base_size = bankroll × tier_pct (0.5%–2% depending on whale score 55–85+)
  confidence_mult = min(1.5, max(0.5, (roi_score/100) × (consistency_score/100) × 2))
  raw_size = base_size × confidence_mult
  copy_size = min(raw_size, max_per_market_exposure, depth_cap)
  copy_size rounded down to nearest $10
"""


def build_prompt(data: dict) -> str:
    status = data["status"]
    metrics = data["metrics"]
    funnel = data["signal_funnel"]
    tiers = data["tier_breakdown"]
    positions: list[dict] = data["positions"]

    total_signals = metrics.get("signals_evaluated", 0)
    executed = metrics.get("signals_executed", 0)
    pass_pct = executed / max(total_signals, 1) * 100

    open_pos = [p for p in positions if p["status"] == "OPEN"]
    closed_pos = [p for p in positions if p["status"] == "CLOSED"]

    lines: list[str] = []

    lines.append("# Whale Bot 24-Hour Simulation Report\n")

    # -- Status --
    lines.append("## Current Bot State")
    lines.append(f"- Mode: {status.get('mode', 'SIMULATION')}")
    lines.append(f"- Bankroll (liquid cash): ${status.get('bankroll', 0):,.2f}")
    lines.append(f"- Peak Bankroll: ${status.get('peak_bankroll', 0):,.2f}")
    lines.append(f"- Drawdown from peak: {status.get('drawdown_pct', 0):.2f}%")
    lines.append(f"- Whitelisted whale wallets: {status.get('whitelist_count', 0)}")
    lines.append("")

    # -- Metrics --
    lines.append("## Performance Metrics")
    lines.append(f"- Signals evaluated: {total_signals:,}")
    lines.append(f"- Signals executed (all gates passed): {executed:,} ({pass_pct:.2f}%)")
    lines.append(f"- Open positions: {metrics.get('total_open', 0)}")
    lines.append(f"- Closed positions: {metrics.get('total_closed', 0)}")
    lines.append(f"- Win rate (closed): {metrics.get('win_rate', 0):.1f}%")
    lines.append(f"- Realized P&L: ${metrics.get('total_realized_pnl', 0):+,.2f}")
    lines.append(f"- Unrealized P&L: ${metrics.get('total_unrealized_pnl', 0):+,.2f}")
    lines.append(f"- Combined P&L: ${metrics.get('total_pnl', 0):+,.2f}")
    lines.append(f"- Avg P&L per closed trade: ${metrics.get('avg_pnl_per_trade', 0):+,.2f}")
    lines.append(f"- Best trade: ${metrics.get('best_trade', 0):+,.2f}")
    lines.append(f"- Worst trade: ${metrics.get('worst_trade', 0):+,.2f}")
    lines.append(f"- Total capital deployed (open): ${metrics.get('total_deployed_usdc', 0):,.2f}")
    lines.append("")

    # -- Signal funnel --
    lines.append("## Signal Funnel (gate rejection breakdown)")
    for gate in funnel:
        count = gate["count"]
        pct = count / max(total_signals, 1) * 100
        lines.append(f"  {gate['gate']:<25} {count:>7,}  ({pct:.1f}%)")
    lines.append("")

    # -- Tier breakdown --
    lines.append("## P&L by Whale Score Tier")
    if tiers:
        for t in tiers:
            lines.append(
                f"  Tier {t['tier']}: {t['closed']} closed | "
                f"win rate {t['win_rate']:.1f}% | "
                f"total P&L ${t['total_pnl']:+,.2f} | "
                f"avg ${t['avg_pnl']:+,.2f}"
            )
    else:
        lines.append("  No tier data yet.")
    lines.append("")

    # -- Closed positions --
    if closed_pos:
        lines.append(f"## Closed Positions (most recent {min(30, len(closed_pos))})")
        for pos in closed_pos[:30]:
            entry = pos.get("entry_price", 0)
            current = pos.get("current_price", entry)
            lines.append(
                f"  [{pos['tier']:>5}]  {pos['market'][:55]!r:<57}  "
                f"entry {entry:.3f} → {current:.3f}  "
                f"P&L ${pos['pnl']:+.2f} ({pos['roi_pct']:+.1f}%)  "
                f"size ${pos['size_usdc']:.0f}"
            )
    else:
        lines.append("## Closed Positions\n  None yet.")
    lines.append("")

    # -- Open positions --
    if open_pos:
        lines.append(f"## Open Positions ({len(open_pos)} total)")
        for pos in open_pos[:20]:
            entry = pos.get("entry_price", 0)
            current = pos.get("current_price", entry)
            lines.append(
                f"  [{pos['tier']:>5}]  {pos['market'][:55]!r:<57}  "
                f"entry {entry:.3f}  current {current:.3f}  "
                f"unrealized ${pos['pnl']:+.2f} ({pos['roi_pct']:+.1f}%)"
            )
        if len(open_pos) > 20:
            lines.append(f"  ... and {len(open_pos) - 20} more")
    lines.append("")

    # -- System description for context --
    lines.append("## System Description")
    lines.append(GATE_DESCRIPTIONS)

    # -- Analysis request --
    lines.append("## Please Analyze\n")
    lines.append(
        "Provide a thorough diagnostic report covering ALL of the following sections:\n"
    )
    lines.append(
        "### 1. Executive Summary\n"
        "In 2–3 sentences: is this strategy working? What is the single most important "
        "thing to fix before the next run?\n"
    )
    lines.append(
        "### 2. Signal Funnel Diagnosis\n"
        "For each gate, assess whether the rejection rate is reasonable or indicates a "
        "miscalibration. Flag any gate that is clearly filtering too aggressively or too "
        "leniently. Suggest specific threshold changes (e.g., 'raise MIN_ENTRY_PRICE from "
        "0.05 to 0.10') with quantitative reasoning.\n"
    )
    lines.append(
        "### 3. Position Quality Analysis\n"
        "Look at the pattern of wins and losses in closed positions. Are there entry price "
        "ranges, market types, sizes, or tiers that consistently win or lose? What does "
        "this tell us about which markets we should avoid?\n"
    )
    lines.append(
        "### 4. Sizing & Bankroll Analysis\n"
        "Is capital being deployed efficiently? Are positions too large or too small? "
        "Is the bankroll utilization rate (deployed / total) healthy? Any issues with "
        "the confidence multiplier or tier percentages?\n"
    )
    lines.append(
        "### 5. Strategy-Level Improvements\n"
        "Beyond gate tuning, what fundamental changes to the copy-trading approach "
        "would most improve outcomes? Consider: exit strategies, position management, "
        "market selection criteria, timing, whale scoring improvements.\n"
    )
    lines.append(
        "### 6. Priority Action List\n"
        "Rank the top 5 specific, implementable changes ordered by expected impact. "
        "For each: what to change, what value to use, and why. "
        "Be honest about what you can and cannot conclude from this data volume.\n"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching data from {args.url} ...")
    try:
        data = await fetch_all(args.url)
    except aiohttp.ClientConnectorError:
        print(
            f"Could not connect to {args.url}.\n"
            "If running locally: start the monitor with `python -m monitor.api`\n"
            "If running against Railway: pass --url https://your-service.railway.app",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"Error fetching data: {exc}", file=sys.stderr)
        sys.exit(1)

    metrics = data["metrics"]
    print(
        f"Got: {metrics.get('signals_evaluated', 0):,} signals evaluated, "
        f"{metrics.get('total_closed', 0)} closed positions, "
        f"{metrics.get('total_open', 0)} open positions."
    )

    prompt = build_prompt(data)

    client = anthropic.Anthropic(api_key=api_key)

    print("\nAnalyzing with Claude Opus 4.6 (adaptive thinking)...\n")
    print("=" * 80)

    report_chunks: list[str] = []
    thinking_shown = False

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for event in stream:
            if event.type == "content_block_start":
                block = event.content_block
                if block.type == "thinking" and not thinking_shown:
                    print("[Claude is thinking deeply...]\n", flush=True)
                    thinking_shown = True
            elif event.type == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    print(delta.text, end="", flush=True)
                    report_chunks.append(delta.text)

        final = stream.get_final_message()

    print("\n" + "=" * 80)
    print(
        f"\nDone. Tokens — input: {final.usage.input_tokens:,}  "
        f"output: {final.usage.output_tokens:,}"
    )

    if args.output:
        report = "".join(report_chunks)
        header = (
            f"# Whale Bot Analysis Report\n"
            f"Generated: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        )
        with open(args.output, "w") as f:
            f.write(header + report)
        print(f"Report saved to: {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze whale bot simulation results with Claude Opus 4.6"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080",
        help="Base URL of the monitoring API (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Save the analysis report to a markdown file",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
