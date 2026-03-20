# Polymarket Whale Bot — Developer Reference

## What it does

Copy-trades large ("whale") positions on [Polymarket](https://polymarket.com) — a
prediction-market exchange running on Polygon. The bot monitors real-time trade
events over the Polymarket CLOB WebSocket, scores them through a 10-gate signal
engine, and either places paper trades (simulation mode, default) or real CLOB
orders (live mode).

Currently deployed on Railway at:
`https://polytest-production.up.railway.app/`

---

## Modes

| Mode | `SIMULATION_MODE` | WebSocket channel | Executes orders |
|---|---|---|---|
| Simulation (default) | `True` | Public market channel — all large trades on pre-screened alpha markets | Never — records virtual positions |
| Live | `False` | Private user channel — whitelisted whale wallets only | Real CLOB orders via API key |

**In simulation mode, wallet_address is set to `"MARKET_TRADE"`** for every event
(no identity). The signal engine detects this sentinel and assigns a synthetic whale
score derived from trade size (large trade = higher score) instead of doing a
whitelist lookup.

---

## Architecture

```
WebSocket (public market or private user channel)
  └─► websocket_stream.py  →  on_trade callback
        └─► main.py _handle_trade()  (non-blocking asyncio.create_task)
              └─► _process_trade_pipeline():
                    1. PositionLedger.update()    — track aggregate market direction
                    2. SignalEngine.evaluate()    — 10-gate filter
                    3. PaperTrader.execute()      — sim: virtual fill from live orderbook
                       OR OrderExecutor.execute() — live: CLOB order
                    4. DB persistence (signal_events, bot_positions, trades)
                    5. TelegramAlerter alerts
```

### Service graph

| Service | File | Purpose |
|---|---|---|
| `WhaleBot` | `main.py` | Orchestrator, wires everything |
| `SignalEngine` | `signals/signal_engine.py` | 10 ordered gate checks |
| `PositionLedger` | `signals/position_ledger.py` | Tracks whale aggregate position per market (Redis) |
| `WhitelistManager` | `scoring/whitelist_manager.py` | Maintains scored whale wallet list (Redis + Postgres) |
| `WhaleScorerService` | `scoring/whale_scorer.py` | Computes composite whale score from Bitquery history |
| `BitqueryClient` | `data/bitquery_client.py` | On-chain CTF transfer data (Bitquery V2 API) |
| `PaperTrader` | `simulation/paper_trader.py` | Simulated VWAP fills against live orderbook |
| `MarketMonitor` | `simulation/market_monitor.py` | Marks open positions to market, triggers stop/TP exits |
| `RiskGate` | `execution/risk_gate.py` | Circuit breaker, drawdown limits |
| `CLOBClient` | `data/clob_client.py` | Polymarket CLOB REST API |
| `GammaClient` | `data/gamma_client.py` | Gamma API — fetches alpha markets for sim mode |
| `TelegramAlerter` | `alerts/telegram_bot.py` | Optional Telegram notifications |
| FastAPI monitor | `monitor/api.py` + `monitor/dashboard.html` | Web dashboard at `/` |

### Databases

- **Postgres** (Railway managed): `signal_events`, `bot_positions`, `bot_orders`,
  `trades`, `wallet_scores`, `daily_pnl_snapshots`
- **Redis** (Railway managed): `sim:bankroll`, `whale:whitelist` (sorted set),
  `bot:latency:samples`, `whale:p90_trade_size`, position ledger keys

---

## Signal pipeline — gate-by-gate

Gates are in `signals/signal_engine.py:evaluate()`. First failure short-circuits.
All rejections are written to `signal_events` for funnel analytics.

| Gate | Label | Condition to pass |
|---|---|---|
| 0 | *(SELL → BUY-NO conversion)* | SELL events: convert to equivalent BUY-NO before Gate 1. Drops if paired token can't be found. |
| 1 | `TRADE_IS_BUY` | Trade side must be BUY after conversion |
| 2 | `TRADE_SIZE_MIN` | `size_usdc >= MIN_WHALE_TRADE_SIZE` (500 USDC) |
| 3 | `PRICE_RANGE` | `MIN_ENTRY_PRICE ≤ price ≤ MAX_ENTRY_PRICE` (0.03–0.90) |
| 4 | `WHALE_SCORE_MIN` | Wallet score ≥ `WHALE_SCORE_FLOOR` (65.0). In sim mode with `MARKET_TRADE`, synthetic score from trade size |
| 5 | `MARKET_OI_MIN` | Open interest ≥ 150,000 USDC (skipped if OI == 0 — happens in sim without auth) |
| 6 | `ORDERBOOK_DEPTH` | Sufficient depth for MIN_COPY_SIZE; computed copy size ≥ 50 USDC |
| 7 | `POSITION_CAP` | Existing market exposure < `MAX_PER_MARKET_EXPOSURE_PCT` (5%) of bankroll |
| 8 | `TIME_TO_RESOLUTION` | ≥ 1h until market closes |
| 8b | `MAX_TIME_TO_RESOLUTION` | ≤ `MAX_HOURS_TO_RESOLUTION` (effectively unlimited — 999999h) |
| 9 | `CIRCUIT_BREAKER` | No active circuit breaker |
| 10 | `CORRELATED_MARKET` | No open position with >50% keyword overlap (Jaccard similarity) |

After all gates pass, `PaperTrader` has two more checks:
- `DUPLICATE_POSITION` — already holding this token
- `PRICE_ASSERTION_FAILED` — live orderbook VWAP fill price not in `[MIN_ENTRY_PRICE, SIM_FILL_PRICE_MAX]` (0.03–0.97)
- `INSUFFICIENT_CASH` — bankroll too low

### Copy sizing formula

```
tier_pct     = TIER_PCT lookup(whale_score)  # 0.5%–2% of bankroll
confidence   = min(1.5, max(0.5, roi_score/100 * consistency_score/100 * 2))
raw_size     = bankroll * tier_pct * confidence
depth_cap    = orderbook_depth_within_2%_slippage * MAX_LIQUIDITY_CONSUMPTION_PCT(20%)
copy_size    = floor(min(raw_size, max_exposure, depth_cap) / 10) * 10  # nearest $10
```

Score tiers: 55–65 → 0.5%, 65–75 → 1%, 75–85 → 1.5%, 85+ → 2%

---

## Whale whitelist

`scoring/whitelist_manager.py:refresh_whitelist()` runs at startup and then daily
at 02:00 UTC via APScheduler.

**Startup flow:**
1. Pull previously scored wallets from `wallet_scores` Postgres table (warm start)
2. Discover new candidates via `BitqueryClient.get_top_trader_wallets()` — queries
   Bitquery for the most active receivers of Polymarket CTF tokens over `LOOKBACK_DAYS`
   (90 days). Returns up to 200 wallets ranked by transfer frequency.
3. Score each candidate via `WhaleScorerService.score_wallet()` using full Bitquery
   trade history. Wallets failing `MIN_RESOLVED_MARKETS=30` or `MIN_TOTAL_VOLUME_USDC=5000`
   raise `InsufficientDataError` and are skipped.
4. **Fallback**: if `scored` is empty (all scoring failed or no Bitquery key),
   assign proxy scores by rank in the candidate list:
   - Rank 1–10 → score 85, 11–30 → 75, 31–60 → 65, 61+ → 58 (below floor, excluded)
5. Filter to `WHALE_SCORE_FLOOR=65`, take top `WHITELIST_MAX_SIZE=75` wallets
6. Persist to Postgres + Redis sorted set (`whale:whitelist`)

**Cold-start note**: On first deploy with an empty DB, the entire whitelist depends
on Bitquery discovery working correctly. The discovery is currently fast (one bulk
query for 1000 top receivers) but scoring is sequential — one Bitquery API call per
wallet. With 200 candidates this takes 10–20 minutes before the whitelist is
populated. In sim mode this doesn't block trade signals (MARKET_TRADE bypass).

---

## Bitquery integration

`data/bitquery_client.py` uses **Bitquery V2** (migrated from V1 in session 2).

| | V1 (old) | V2 (current) |
|---|---|---|
| Endpoint | `https://graphql.bitquery.io` | `https://streaming.bitquery.io/graphql` |
| Auth | `X-API-KEY: {key}` header | `Authorization: Bearer {key}` |
| Query syntax | `ethereum(network: matic) { transfers { ... } }` | `EVM(network: matic) { Transfers { ... } }` |
| Response keys | lowercase (`data.ethereum.transfers`) | PascalCase (`data.EVM.Transfers`) |
| Date format | ISO datetime string | `YYYY-MM-DD` date string |

**API key format**: The key starts with `ory_at_` — this is a Bitquery V2 OAuth
token. V1 endpoint rejects it (401). Always use V2.

**CTF token decimals**: Raw `Amount` field from Bitquery is in 6-decimal units.
Always divide by `1_000_000` to get whole shares.

**Contracts tracked**:
- CTF: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- NegRisk CTF: `0xC5d563A36AE78145C45a50134d48A1215220f80a`

---

## Key configuration (`config/settings.py`)

All values come from environment variables (Railway env vars or `.env` file).

| Setting | Current value | Notes |
|---|---|---|
| `SIMULATION_MODE` | `True` | Must be `False` + all POLYMARKET_* creds for live |
| `SIM_BANKROLL_USDC` | 10,000 | Virtual starting capital |
| `MIN_WHALE_TRADE_SIZE` | 500 USDC | Gate 2 threshold |
| `MIN_ENTRY_PRICE` | 0.03 | Gate 3 + paper trader lower bound |
| `MAX_ENTRY_PRICE` | 0.90 | Gate 3 upper bound |
| `SIM_FILL_PRICE_MAX` | 0.97 | Paper trader upper bound (wider than MAX_ENTRY_PRICE) |
| `MAX_HOURS_TO_RESOLUTION` | 999999 | Effectively unlimited |
| `WHALE_SCORE_FLOOR` | 65.0 | Minimum score to trade |
| `MIN_COPY_SIZE` | 50 USDC | Minimum position size |
| `MAX_PER_MARKET_EXPOSURE_PCT` | 5% | Max bankroll per market |
| `MAX_DRAWDOWN_PCT` | 15% | Circuit breaker trigger |
| `SIM_STOP_LOSS_PCT` | 30% | Auto-close on loss |
| `SIM_TAKE_PROFIT_PCT` | 50% | Auto-close on gain |

---

## Scheduled jobs (APScheduler)

| Job | Schedule | What it does |
|---|---|---|
| Whitelist refresh | 02:00 UTC daily | Re-discover and re-score whale wallets |
| Sim mark-to-market | Every 15 min | Update `current_price` on open positions |
| Sim performance report | Every 6h | Send P&L summary to Telegram |
| Sim daily snapshot | 23:55 UTC daily | Persist daily P&L snapshot to DB |

---

## Monitoring dashboard

FastAPI app served on `PORT` (default 8080). Dashboard at `/`.

Key API endpoints:
- `GET /api/status` — bankroll, position count, whitelist count, latency
- `GET /api/metrics` — P&L, win rate, daily returns
- `GET /api/positions` — open positions
- `GET /api/funnel` — signal gate rejection breakdown
- `GET /api/whitelist?limit=20` — whale leaderboard data
- `GET /api/tiers` — performance broken down by score tier

**Analysis script**: `python scripts/analyze_results.py --url https://polytest-production.up.railway.app/`
Prints a funnel breakdown of all signal rejections with counts and percentages.

---

## Deployment

Deployed on Railway. Auto-deploys on push to `main` branch of the `polytest` remote:

```bash
git push polytest HEAD:main
```

Remote URL: `https://github.com/odbartlett/polytest.git`

Required Railway environment variables:
- `DATABASE_URL` — Postgres connection string (Railway auto-sets)
- `REDIS_URL` — Redis connection string (Railway auto-sets)
- `BITQUERY_API_KEY` — Bitquery V2 OAuth token (`ory_at_...`)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — optional alerts
- `POLYMARKET_*` credentials — only for live mode

---

## Current issues and recent fixes

### Fixed this session

| Issue | Root cause | Fix |
|---|---|---|
| Zero trades from 190k signals evaluated | `datetime.utcfromtimestamp()` returns timezone-naive datetime; subtracting from `datetime.now(tz=timezone.utc)` raises `TypeError` on every trade, silently caught | Changed to `datetime.fromtimestamp(..., tz=timezone.utc)` in `websocket_stream.py` |
| 61% of signals killed by `PRICE_ASSERTION_FAILED` | Paper trader VWAP fill > `SIM_FILL_PRICE_MAX=0.93` | Widened to `SIM_FILL_PRICE_MAX=0.97`; added `TOO_HIGH/TOO_LOW` direction logging |
| 33% of signals killed by `PRICE_RANGE` | `MAX_ENTRY_PRICE=0.85` too tight | Widened to `0.90` |
| 5% of signals killed by `MAX_TIME_TO_RESOLUTION` | Cap was 90 days (2160h) | Set to 999999 (unlimited) |
| Bitquery returning no data | Key prefix `ory_at_` is V2 OAuth; code used V1 endpoint + `X-API-KEY` auth | Migrated entire client to V2 endpoint, Bearer auth, V2 GraphQL schema |
| Whale leaderboard blank on startup | Whitelist empty at startup (discovery bug + cold DB) | Fixed leaderboard API `window` param from `"monthly"` (invalid) to `"1m"`; improved dashboard message |

### Known open issues

**Whitelist scoring is sequential** (`whitelist_manager.py:80`): the scoring loop
iterates wallets one-at-a-time with no parallelism. With 200 candidates and ~3s per
Bitquery call, cold-start scoring takes 10–20 minutes. The fallback proxy scoring is
instant but provides no real signal quality differentiation. Fix: parallelize with
`asyncio.gather` and a semaphore.

**USDC cost approximation** (`bitquery_client.py:377`): `size_usdc = token_amount * 0.5`
is a rough midpoint approximation for binary markets. Actual cost requires correlating
each CTF transfer with a USDC transfer in the same transaction (secondary query). This
affects whale scorer accuracy but not real-time signal evaluation (which uses live
orderbook data).

**Paper trader fill assertion direction** (`simulation/paper_trader.py`): added
`TOO_HIGH/TOO_LOW` direction logging to the `PRICE_ASSERTION_FAILED` warning. After
the threshold widening deploy, monitor logs to confirm `PRICE_ASSERTION_FAILED` rate
drops. If still failing `TOO_HIGH`, the market has illiquid orderbooks; if `TOO_LOW`,
whales are buying near-certain outcomes.

**Sim mode whitelist irrelevance**: In simulation mode all trades arrive as
`MARKET_TRADE` (public channel, no wallet identity), so the whitelist doesn't affect
trade generation. Whitelist matters only when switching to live mode. The leaderboard
panel on the dashboard will populate once Bitquery V2 discovery runs successfully.
