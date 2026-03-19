# Whale Bot Analysis Report
Generated: 2026-03-19 05:21 UTC

# Whale Bot Simulation — Diagnostic Report

---

## 1. Executive Summary

**This strategy is critically broken.** Every one of the 17 open positions was entered at a price of $0.999–$1.000 — the theoretical maximum — meaning the bot bought tokens priced at near-certainty on events that are manifestly uncertain (e.g., "Will Bitcoin dip to $35,000 in March?" at $1.00). This guarantees losses; there is zero upside and maximum downside. The PRICE_RANGE gate (0.05–0.70) should have blocked every one of these trades, so there is either a price-field bug or a YES/NO token-side inversion in the execution pipeline. **The single most important fix is to identify and resolve why the PRICE_RANGE gate is passing tokens priced at $1.00.** Nothing else matters until this is fixed, because the bot is systematically buying at the worst possible price on every trade.

---

## 2. Signal Funnel Diagnosis

Reconstructing the waterfall from 413,084 inbound signals:

| Gate | Rejected | Passed Through | Rejection Rate (of input) | Assessment |
|---|---|---|---|---|
| TRADE_IS_BUY | 190,030 | 223,054 | 46.0% | ✅ **Normal.** ~54% buys / 46% sells is a typical prediction-market mix. |
| TRADE_SIZE_MIN (≥$500) | **0** | 223,054 | 0.0% | 🔴 **Suspicious.** Zero rejections means every buy-side signal is ≥$500. Either the data feed is pre-filtered to large trades (in which case this gate is redundant) or the gate is not evaluating. Verify the gate is reading the correct field. |
| PRICE_RANGE (0.05–0.70) | 173,702 | 49,352 | 77.8% | 🔴 **Broken.** The gate rejects 78% of what it sees — but every surviving trade entered at $1.00. The gate is clearly checking a different price field than the one used for execution. Likely it's checking the *market probability* or the *opposite side's price* while the execution engine buys the complementary token. See detailed analysis below. |
| WHALE_SCORE_MIN (≥55) | **0** | 49,352 | 0.0% | 🟡 **Too lenient or pre-filtered.** If the feed already only includes known wallets ≥55, the gate is a no-op. If not, zero rejections from 49K signals means the threshold is too low. |
| MARKET_OI_MIN (≥$50K) | **0** | 49,352 | 0.0% | 🟡 **Likely too low.** $50K OI on prediction markets is very thin. Many of the positions (obscure Peruvian elections, specific-politician nomination bets) are in markets that likely have minimal real liquidity. Raise to ≥$200K. |
| ORDERBOOK_DEPTH | 34,158 | 15,194 | 69.2% | ✅ **Working and aggressive.** Rejects ~69% for insufficient depth. This is doing real filtering, though clearly not enough given the positions we see. |
| POSITION_CAP (<5% bankroll) | **0** | 15,194 | 0.0% | 🟡 **Plausible** if positions are small enough, but suspicious given 12,478 executions and only 17 positions (see below). |
| TIME_TO_RESOLUTION (≥6h) | 2,716 | 12,478 | 17.9% | ✅ **Reasonable.** Appropriately filters near-expiry markets. |
| CIRCUIT_BREAKER (<15% DD) | **0** | 12,478 | 0.0% | 🟡 **See Section 4** — the drawdown calc appears to use cash-only, not total portfolio, which inflates the reported drawdown. Currently at 12.2% (cash-based) when real drawdown is ~5.6%. |

### The PRICE_RANGE Bug (Critical)

All 17 positions have entry prices of $0.999–$1.000. The PRICE_RANGE gate of 0.05–0.70 should make this impossible. The most probable explanations:

1. **YES/NO Token Inversion.** A whale buys YES on "Will Bitcoin dip to $35K?" at $0.03 (cheap long-shot bet). This passes the 0.05–0.70 check... actually no, $0.03 < $0.05, so it wouldn't. But if the gate checks the whale's trade price and the whale bought at, say, $0.50, the gate passes, and then the bot buys the **NO** token at $0.50 or the YES token at the **ask** which has slipped to $1.00 by execution time — that would produce entries at $1.00.

2. **Field Mismatch.** The gate checks `token.lastTradePrice` (which could be low) while execution uses `token.askPrice` or `token.bestOffer` (which could be at $1.00 in an illiquid book).

3. **Stale Price vs. Execution Price.** The gate evaluates at signal time using an old price snapshot, but by the time the order is placed, the price has moved to $1.00 (perhaps because other copy-bots front-ran the same whale signal).

**Specific recommendation:** Add a **pre-execution price assertion** — immediately before submitting the order, re-check the actual fill price and abort if it's outside 0.05–0.70. Log the gate-evaluation price alongside the execution price for every trade to identify the discrepancy.

### The 12,478 Executed vs. 17 Positions Discrepancy

12,478 signals "passed all gates" but only 17 positions were opened (0 closed). This 733:1 ratio means either:
- "Executed" is mislabeled and means "passed all gates" but an additional step (actual order submission / fill) only succeeds rarely
- Many signals target the same market and de-duplicate to one position
- There's an off-book filter not shown in the gate list

**Recommendation:** Add instrumentation to track exactly how many unique orders are submitted and filled. The current logging creates a false impression of 12,478 trades.

---

## 3. Position Quality Analysis

### What We Can Observe (No Closed Positions)

With zero closed trades, we cannot compute win rate, Sharpe, or any statistical measure of strategy quality. All we have is the 17 open positions. However, the pattern is so uniform that it's diagnostic:

| Pattern | Count | Observation |
|---|---|---|
| Entry at $0.999–$1.000 | 17/17 (100%) | **Every position entered at max price.** This is not a market selection problem — it's a mechanical bug. |
| Current price at $0.500 | 16/17 (94%) | The $0.500 values may be a default/placeholder for illiquid markets with no recent trades, not real market prices. |
| Unrealized loss of ~50% | 16/17 | Structurally guaranteed when buying at $1.00 and marking at $0.50. |
| Whale score 55–65 tier | 11/17 (65%) | The majority of positions come from the lowest-quality whales (score 55–65). |
| Whale score 85+ tier | 1/17 (6%) | Only one trade from a top-tier whale — and it's the worst single position (-$100). |

### Market Type Analysis

| Category | Positions | Total Unrealized |
|---|---|---|
| Obscure political nominations (2028 presidential) | 5 | -$224.96 |
| Foreign elections (Peru, Denmark) | 3 | -$75.00 |
| Bitcoin price targets | 4 | -$129.77 |
| Federal Reserve / macro | 2 | -$25.10 |
| Geopolitical (Trump-Putin meeting) | 2 | -$50.00 |
| Other | 1 | -$25.00 |

The bot is heavily overexposed to **long-horizon, low-probability events** (2028 presidential races, specific BTC price dips) — exactly the kind of markets where tokens for extreme outcomes should cost $0.01–$0.10, not $1.00. This confirms the price inversion hypothesis.

### Duplicate Positions

There are two separate entries for "Will Bitcoin dip to $35,000 in March?" and two for the Danish Social Democrats question. The POSITION_CAP gate (5% of bankroll = $500) should have caught the second entry. The fact that it didn't (and shows 0 rejections) suggests it's either not checking correctly or using a different market identifier.

---

## 4. Sizing & Bankroll Analysis

### Capital Utilization

| Metric | Value | Assessment |
|---|---|---|
| Total bankroll (starting) | $10,000 | — |
| Liquid cash | $8,780 | — |
| Capital deployed (at cost) | $1,220 | 12.2% of starting bankroll |
| Capital at risk (mark-to-market) | $660.17 | 6.6% of starting bankroll |
| True portfolio value | ~$9,440.17 | $8,780 + $660.17 |
| True drawdown | **~5.6%** | Not 12.2% as reported |

**Capital utilization is actually low (12.2%)**, which would be fine for a selective strategy — but here it just reflects how few positions the broken pipeline opens.

### Drawdown Calculation Bug

The system reports drawdown as:

$$\text{Reported DD} = \frac{\$10{,}000 - \$8{,}780}{\$10{,}000} = 12.2\%$$

This uses **liquid cash only**, ignoring the mark-to-market value of open positions. The correct calculation:

$$\text{True DD} = \frac{\$10{,}000 - \$9{,}440.17}{\$10{,}000} = 5.6\%$$

The circuit breaker at 15% will fire at a **true portfolio loss of only ~8–9%** because deployed capital is double-counted as "lost." If the bot had 30% utilization, it would trip the breaker even with zero unrealized losses. **Fix the drawdown to use total portfolio value** (liquid + mark-to-market of open positions).

### Per-Position Sizing

| Whale Tier | Tier % | Base Size ($10K) | Positions | Avg Position Size |
|---|---|---|---|---|
| 85+ | 2.0% | $200 | 1 | $200 |
| 65–75 | ~1.0% | $100 | 4 | $100–$110 |
| 55–65 | ~0.5% | $50 | 12 | $50 |

The sizes are appropriately scaled by tier, but the confidence multiplier doesn't appear to be doing meaningful differentiation — most positions land at exactly the base amount ($50 or $100), suggesting confidence_mult ≈ 1.0 for most whales. This makes sense if `roi_score` and `consistency_score` are both near 70–71 (since 0.70 × 0.70 × 2 ≈ 0.98 ≈ 1.0). The multiplier formula may need recalibration to actually discriminate.

---

## 5. Strategy-Level Improvements

### 5.1 Exit Strategy (Missing Entirely)

There is **no exit strategy.** The bot buys and holds until resolution. For a copy-trading bot, this is a critical omission. You need:

- **Copy-exit signals:** If the whale you copied sells their position, you should sell too — this is arguably the highest-value signal available.
- **Stop-loss:** A per-position stop at -20% to -30% would have limited the damage here. Currently positions sit at -50% indefinitely.
- **Profit-taking:** If a position reaches +30% to +50% unrealized, take partial or full profit rather than waiting for binary resolution.
- **Time-decay exit:** As resolution approaches and price hasn't moved, exit to free capital.

### 5.2 Sell-Side Signals

The TRADE_IS_BUY gate discards 46% of all signals. Whale **sells** are extremely valuable:
- A whale selling a position you hold is a direct exit signal
- A whale selling a token can be converted into a contrarian buy of the opposite outcome
- Large sells can signal informed bearishness

**Recommendation:** Don't blindly copy sells, but use whale sell signals for: (a) exiting positions you already hold, and (b) evaluating whether to buy the other side.

### 5.3 Market Selection Heuristic

Even after fixing the price bug, the bot needs a market-quality filter. The current positions are in:
- **2028 presidential elections** (resolution 3+ years away — capital locked for an eternity)
- **Peruvian elections** (likely very thin order books, wide spreads)
- **Specific BTC price targets in a single month** (binary gambles)

Add a filter for:
- **Maximum time-to-resolution:** Cap at 90 days. Tying up capital for years is terrible for capital efficiency.
- **Minimum market volume (trailing 24h):** At least $10K in 24h volume, not just OI. A market can have $50K OI with zero daily volume, making it impossible to exit.
- **Market category whitelist:** Start with only high-liquidity categories (major elections within 6 months, crypto milestones, sports, economic indicators).

### 5.4 Whale Selection

With 0 whitelisted wallets but whale scores being evaluated, the system appears to score any wallet dynamically. The problem is that scores 55–65 (barely above threshold) account for 65% of positions. These are low-conviction signals.

**Recommendation:** Raise WHALE_SCORE_MIN to 70 and increase tier sizing differentiation. The current approach of taking many small bets on mediocre whales dilutes capital that should be concentrated on the few genuinely skilled traders.

### 5.5 Slippage & Front-Running Awareness

When 12,478 signals "execute" from the same pool, the bot may be attempting to buy into markets that other copy-bots are simultaneously hitting. This creates adverse price impact. Consider:
- Adding a **delay and re-check** — wait 30–60 seconds after signal, then re