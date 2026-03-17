# Polymarket Whale Bot

A production-grade Python bot that monitors Polymarket's top traders ("whales"), scores them by historical performance, and automatically copy-trades their most confident entries.

---

## Architecture Overview

```
                        ┌──────────────────────────────────┐
                        │         Polymarket CLOB           │
                        │    (REST API + WebSocket)         │
                        └────────┬─────────────────────────┘
                                 │ TradeEvent stream
                                 ▼
                        ┌────────────────┐
                        │  websocket_    │
                        │  stream.py     │◄──── Whitelisted wallet addresses
                        └────────┬───────┘
                                 │ on_trade callback
                                 ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         Signal Pipeline (async)                          │
│                                                                          │
│  PositionLedger.update()   →   SignalEngine.evaluate()                  │
│      (Redis-backed)             (8 gate checks)                          │
│           │                          │                                   │
│           │ TradeClassification      │ SignalDecision                    │
│           ▼                          ▼                                   │
│      [ENTRY/ADD/             RiskGate.check()                           │
│       EXIT/FLIP/              (circuit breaker,                          │
│       NOISE]                   exposure cap)                             │
│                                      │                                   │
│                                      ▼                                   │
│                          OrderExecutor.execute()                         │
│                           (CLOB limit order)                             │
│                                      │                                   │
│                                      ▼                                   │
│                           TelegramAlerter                                │
└──────────────────────────────────────────────────────────────────────────┘
         ▲
         │ nightly refresh
┌────────┴────────────┐          ┌──────────────┐
│  WhitelistManager   │◄────────►│   Bitquery   │
│  (scoring + rank)   │          │  (GraphQL)   │
└─────────────────────┘          └──────────────┘
         │
         ▼
┌────────────────┐    ┌────────────────┐
│   Postgres     │    │    Redis       │
│  (all state)   │    │  (hot cache,   │
└────────────────┘    │   ledger,      │
                      │   bankroll)    │
                      └────────────────┘

Scheduled jobs (APScheduler):
  02:00 UTC  — whitelist refresh
  23:55 UTC  — daily P&L snapshot + Telegram summary
  every 15s  — monitor open CLOB orders
  every 60s  — bankroll sync from realized P&L
  every 5min — cancel stale/expired orders
```

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11+ |
| Docker + Docker Compose | v2+ |
| Polymarket account | With CLOB API key enabled (US restrictions apply — see Limitations) |
| Bitquery API key | Free tier available at bitquery.io |
| Telegram bot | Created via @BotFather; get chat ID from @userinfobot |
| Polygon wallet | With MATIC for gas (small amounts) and USDC for trading |

---

## Setup

```bash
# 1. Clone the repository
git clone https://github.com/yourname/polymarket-whale-bot.git
cd polymarket-whale-bot

# 2. Copy and fill in credentials
cp .env.example .env
$EDITOR .env

# 3. Start all services
docker-compose up -d

# 4. View logs
docker-compose logs -f bot
```

That's it. On first startup the bot will:
1. Run database migrations
2. Perform an initial whitelist refresh (may take several minutes)
3. Connect to the Polymarket WebSocket
4. Send a Telegram startup notice

---

## How the Whale Scoring Algorithm Works

Each candidate wallet is scored on five dimensions (0-100 each), then combined into a single **whale score** using a weighted average.

### 1. ROI Score (weight: 35%)

Measures raw profit over cost across all *resolved* markets.

```
realized_roi = (total_payout - total_cost) / total_cost
roi_score    = min(100, max(0, realized_roi x 200))
```

A wallet that doubles its money (100% ROI) scores 100. A wallet that breaks even scores 0.

### 2. Consistency Score (weight: 25%)

Bayesian-adjusted win rate -- prevents small-sample flukes from scoring highly.

```
consistency_score = (wins + 15) / (resolved + 30) x 100
```

The prior of 15 pseudo-wins / 30 pseudo-trades shrinks extreme estimates for wallets with limited history toward the population mean (~50%).

### 3. Sizing Score (weight: 20%)

Measures trade size relative to the platform-wide 90th percentile (P90).
Larger trades signal higher conviction and better information.

```
sizing_score = min(100, (median_trade_size / p90_platform_size) x 100)
```

P90 is computed nightly from all scored wallets and cached in Redis.

### 4. Specialization Score (weight: 10%)

Identifies wallets that are significantly better than average in a specific category
(POLITICS, CRYPTO, SPORTS, ECONOMICS, SCIENCE_GEO, OTHER).
Only categories with >= 5 resolved trades are considered.

```
specialization_score = max_category_win_rate x 100
```

### 5. Recency Score (weight: 10%)

Emphasises recent profit over older profit using exponential decay.

```
weighted_profit  = sum[ profit(t) x exp(-L x days_since_resolution) ]
recency_score    = min(100, (weighted_profit / undiscounted_profit) x 100)
```

L = 0.02 by default (approximately 35-day half-life).
If undiscounted total profit <= 0, recency_score = 0.

### Composite

```
whale_score = 0.35 x roi
            + 0.25 x consistency
            + 0.20 x sizing
            + 0.10 x specialization
            + 0.10 x recency
```

Only wallets scoring >= 55 enter the whitelist. The top 75 by score are tracked.

---

## How the Signal Pipeline Works

When a whitelisted whale places a trade, it is evaluated against **8 sequential gates**. The first failure short-circuits evaluation.

| Gate | Check |
|---|---|
| 1. TRADE_IS_BUY | Only buy-side trades are copied |
| 2. TRADE_SIZE_MIN | Whale trade must be >= MIN_WHALE_TRADE_SIZE ($500) |
| 3. WHALE_SCORE_MIN | Wallet score must be >= WHALE_SCORE_FLOOR (55) |
| 4. MARKET_OI_MIN | Market open interest must be >= $50k |
| 5. ORDERBOOK_DEPTH | Sufficient depth within slippage tolerance |
| 6. POSITION_CAP | Bot's existing exposure in this market < 5% of bankroll |
| 7. TIME_TO_RESOLUTION | Market resolves > 6 hours from now |
| 8. CIRCUIT_BREAKER | Bot drawdown < 15% |

### Copy Sizing

If all gates pass, the copy size is computed as:

```
tier_pct        = { 55-65: 0.5%, 65-75: 1.0%, 75-85: 1.5%, 85-100: 2.0% }
base_size       = bankroll x tier_pct[whale_score]
confidence_mult = clamp(roi_score/100 x consistency_score/100 x 2.0, 0.5, 1.5)
raw_size        = base_size x confidence_mult
copy_size       = min(raw_size, max_per_market_cap, orderbook_depth x 20%)
copy_size       = floor(copy_size / 10) x 10   # round to nearest $10
```

---

## Risk Parameter Reference

| Parameter | Default | Description |
|---|---|---|
| `BANKROLL_USDC` | 1000 | Starting capital |
| `MAX_PER_MARKET_EXPOSURE_PCT` | 5% | Max capital in any single market |
| `MAX_DRAWDOWN_PCT` | 15% | Circuit breaker threshold |
| `SLIPPAGE_TOLERANCE_LIQUID` | 2% | Max acceptable slippage in liquid books |
| `SLIPPAGE_TOLERANCE_THIN` | 1% | Max acceptable slippage in thin books |
| `ORDER_FILL_TIMEOUT_SECONDS` | 90 | Cancel unfilled limit orders after this |
| `MIN_MARKET_OPEN_INTEREST` | $50,000 | Minimum market liquidity |
| `MIN_WHALE_TRADE_SIZE` | $500 | Minimum trade to trigger signal evaluation |
| `MIN_COPY_SIZE` | $50 | Minimum copy trade size |
| `MIN_HOURS_TO_RESOLUTION` | 6h | Minimum time before market resolves |
| `MAX_LIQUIDITY_CONSUMPTION_PCT` | 20% | Max fraction of orderbook depth consumed |
| `WHALE_SCORE_FLOOR` | 55 | Minimum score for whitelist entry |
| `WHALE_SCORE_REMOVAL` | 45 | Score below which wallet is removed from whitelist |
| `WHITELIST_MAX_SIZE` | 75 | Maximum tracked whale wallets |
| `MIN_RESOLVED_MARKETS` | 30 | Minimum resolved markets to score a wallet |
| `MIN_TOTAL_VOLUME_USDC` | $5,000 | Minimum trading volume to consider |
| `MIN_TRADE_COUNT` | 20 | Minimum trade count to score a wallet |
| `LOOKBACK_DAYS` | 90 | Historical window for scoring |
| `RECENCY_DECAY_LAMBDA` | 0.02 | Exponential decay rate for recency scoring |

---

## Monitoring

### Telegram Alerts

| Emoji | Event |
|---|---|
| Whale | Whale entry detected |
| Check | Copy trade executed |
| Skip | Trade skipped (with reason) |
| Money | Order filled |
| Clock | Order expired unfilled |
| Chart | Position closed (with P&L) |
| Siren | Circuit breaker triggered (CRITICAL) |
| Cycle | Whitelist refresh complete |
| Up | Daily P&L summary |
| Error | Component error |
| Green | Bot started |
| Red | Bot shutdown |

### Log Output

All logs are JSON-structured via `structlog`. Each entry includes:
- `timestamp` (ISO 8601)
- `level` (info/warning/error/critical)
- `component` (module name)
- Context fields (wallet, market_id, size_usdc, etc.)

```bash
# Follow live logs in Docker
docker-compose logs -f bot

# Filter for errors only
docker-compose logs -f bot | jq 'select(.level == "error")'

# Filter for filled orders
docker-compose logs -f bot | jq 'select(.event == "executor.order_filled")'
```

### Circuit Breaker Reset

If the circuit breaker trips, all trading halts automatically. To resume:

```bash
# Via Redis CLI inside Docker
docker-compose exec redis redis-cli SET bot:circuit_breaker_active 0
```

---

## Known Limitations and Caveats

### Regulatory / Geographic Restrictions

Polymarket is geo-restricted in the United States and certain other jurisdictions.
Using a VPN to circumvent these restrictions may violate Polymarket's Terms of Service.
**Consult legal counsel before deploying this bot from a restricted region.**

### No Guaranteed Fills

Limit orders on the CLOB are subject to market conditions. Orders may:
- Expire unfilled if the price moves away
- Fill at a worse price if the book is thin
- Be cancelled by the exchange in edge cases

The bot handles all of these scenarios but cannot guarantee execution.

### Past Performance Disclaimer

The whale scoring algorithm is based on historical data. Past performance of any
wallet -- however highly scored -- does not guarantee future results. Markets can
resolve unexpectedly; information advantage erodes over time; and copy-trading
inherently lags the original trade.

**This software is provided for educational purposes. Use at your own risk.
The authors accept no responsibility for financial losses.**

### Data Latency

- WebSocket trade events are near-real-time but may be delayed by 1-5 seconds.
- Bitquery data has a variable lag (typically 1-30 minutes on-chain).
- Whitelist scores are updated once per day -- a whale's score may not reflect
  very recent trades.

### Market Maker Exclusion Heuristic

The market maker filter (imbalance threshold < 15%) is a heuristic and may
incorrectly exclude directional traders who also provide liquidity.

### Rate Limits

Both the Polymarket CLOB API and Bitquery impose rate limits. The bot implements
exponential backoff but high-frequency trading may exhaust allowances.
