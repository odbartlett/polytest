# Whale Bot Analysis Report
Generated: 2026-03-19 05:36 UTC

# Whale Bot Diagnostic Report

---

## 1. Executive Summary

**This strategy has a critical bug that is destroying capital.** Every single open position was entered at a price between 0.991 and 1.000, meaning the bot is buying tokens priced as near-certainties — yielding at most 0.9% upside against up to 100% downside. The PRICE_RANGE gate (configured for 0.05–0.70) is clearly malfunctioning, because no entry at 0.991+ should have passed it. **The single most important fix is diagnosing and correcting the PRICE_RANGE gate logic before the next run** — it appears to be inverted, rejecting the good range and admitting the catastrophic range.

---

## 2. Signal Funnel Diagnosis

Reconstructing the waterfall (signals entering each gate → rejected at that gate):

| Gate | Entered | Rejected | Rejection Rate | Assessment |
|---|---|---|---|---|
| TRADE_IS_BUY | 415,356 | 191,022 | 46.0% | ✅ Reasonable |
| TRADE_SIZE_MIN | 224,334 | 0 | 0.0% | ⚠️ Suspiciously zero |
| PRICE_RANGE | 224,334 | 174,741 | 77.9% | 🔴 **BROKEN** |
| WHALE_SCORE_MIN | 49,593 | 0 | 0.0% | ⚠️ Suspiciously zero |
| MARKET_OI_MIN | 49,593 | 0 | 0.0% | ⚠️ Suspiciously zero |
| ORDERBOOK_DEPTH | 49,593 | 34,234 | 69.0% | ⚠️ Very aggressive |
| POSITION_CAP | 15,359 | 0 | 0.0% | 🔴 **NOT WORKING** |
| TIME_TO_RESOLUTION | 15,359 | 2,716 | 17.7% | ✅ Reasonable |
| MAX_TIME_TO_RES | 12,643 | 7 | 0.06% | ✅ Fine |
| **EXECUTED** | — | **12,636** | — | — |

### Gate-by-gate assessment:

**TRADE_IS_BUY (46.0% rejection):** Roughly half of whale signals are sells. Normal for a two-sided market. No change needed.

**TRADE_SIZE_MIN (0 rejections):** Zero signals rejected from a $500 minimum out of 224k signals? This means every single non-sell whale trade is ≥$500. Either the whale pool trades very large, or this gate is not being evaluated. **Recommendation: Add logging to verify this gate fires. Consider raising to $1,000–$2,000 to filter for higher-conviction whale bets.**

**PRICE_RANGE (77.9% rejection — CRITICAL BUG):** This is the broken heart of the system. The gate is configured for 0.05–0.70, yet every open position has an entry price of 0.991–1.000. Two possible failure modes:

1. **Inverted logic:** The condition reads `price < 0.05 OR price > 0.70` (reject if IN range) instead of `price >= 0.05 AND price <= 0.70` (accept if IN range). This would perfectly explain the data — 77.9% of buy signals have prices in 0.05–0.70 (rejected), while the 22.1% at extreme prices (near 0 or near 1) pass through.

2. **Wrong field:** The gate checks the YES-token market price while the bot buys NO tokens, or it checks a different field entirely (e.g., a normalized probability rather than execution price).

**Evidence the gate is inverted:** 174,741 / 224,334 = 77.9% of buy signals are in the 0.05–0.70 range — this is the healthy range on a prediction market, so the majority falling there is expected. The remaining 22.1% at extreme prices pass through, and all 20 actual positions confirm entries at 0.991–1.000.

**Fix:** Inspect the conditional logic character-by-character. The correct implementation should be:
```
PASS if entry_price >= 0.05 AND entry_price <= 0.70
REJECT otherwise
```

**WHALE_SCORE_MIN (0 rejections):** Zero rejections at a ≥55 threshold means every whale trade that passed price filtering already has score ≥55. If you're pulling from a curated whale list, this could be fine. But with 0 whitelisted wallets, this suggests either the scoring system inflates scores or the whale universe is pre-filtered. **Recommendation: Verify score distribution. If median score is >70, the ≥55 threshold is doing nothing. Consider raising to ≥65 or ≥70.**

**MARKET_OI_MIN (0 rejections):** Zero rejections at $50,000 OI means the bot only sees signals from liquid markets. This could be correct if the data feed is already limited to major markets. No immediate change, but verify it's actually being checked.

**ORDERBOOK_DEPTH (69.0% rejection):** This is the most aggressive functional gate — rejecting 34,234 of 49,593 signals. This is doing heavy lifting, but because it's working downstream of a broken PRICE_RANGE gate, it's filtering the wrong population. After fixing PRICE_RANGE, re-evaluate whether the depth threshold is appropriate. **Currently, this gate is likely the only thing preventing even more extreme losses.** If depth is thin at near-1.00 prices (because there's no rational counterparty), it's accidentally being a useful filter.

**POSITION_CAP (0 rejections — BUG):** Configured as "already-open exposure in this market < 5% of bankroll" (= $500 on a $10k bankroll). Yet there are duplicate positions:
- 2× "Will Rachida Dati win the Paris mayor election?" 
- 2× "Will Bitcoin dip to $35,000 in March?" ($55 each = $110 total)
- 2× "Will Social Democrats win the most seats in the Danish election?"
- 2× Trump-Putin meeting variants (different markets, so this may be valid)

With 12,636 executed signals but only 20 open positions and $1,790 deployed, the POSITION_CAP either isn't working or 12,636 "executions" aren't real fills. **This discrepancy needs investigation.** If 12,636 trades actually filled, at even $10 minimum the bot should have deployed $126,360. The math doesn't work.

**TIME_TO_RESOLUTION (17.7% rejection):** Reasonable. The 6-hour minimum correctly filters out markets about to close. However, given that positions are in markets resolving in 2026–2028, the concern is the opposite — there's no *maximum* time-to-resolution gate (MAX_TIME_TO_RESOLUTION rejected only 7 signals). The bot is tying up capital in markets years from resolution.

---

## 3. Position Quality Analysis

**Zero closed positions means we cannot calculate win rate, edge, or any statistically meaningful performance metric.** All analysis below is based on unrealized P&L, which is indicative but not definitive.

### Entry Price Distribution (CRITICAL PROBLEM)

| Entry Price | Count | Avg Unrealized | Risk/Reward |
|---|---|---|---|
| 1.000 | 16 | -$30.00 (-48.1%) | 0% upside / 100% downside |
| 0.991 | 3 | -$0.03 (-0.03%) | 0.9% upside / 99.1% downside |
| 0.999 | 1 | -$49.96 (-50.0%) | 0.1% upside / 99.9% downside |

**Every single entry has a terrible risk/reward profile.** Entering at 1.000 is mathematically the worst possible trade — you pay the maximum price and can only lose money. The positions entered at 0.991 (Rachida Dati) are only flat because the market hasn't moved yet, not because they're good entries.

### Market Type Analysis

| Category | Positions | Unrealized P&L | Pattern |
|---|---|---|---|
| 2028 US Presidential | 4 | -$150.00 | Extreme long-duration, highly uncertain |
| Foreign elections (Peru, Denmark, France) | 5 | -$100.00 | Thin markets, speculative |
| Bitcoin price targets | 4 | -$129.77 | Binary bets at extreme prices |
| Fed/Macro | 2 | -$25.10 | Mixed |
| Geopolitical (Trump-Putin) | 2 | -$50.00 | Speculative event markets |

**What the bot is actually doing:** Copying whales who buy NO tokens on highly unlikely outcomes (Jon Stewart as president, Bitcoin at $30k). The whale is probably getting these NO tokens at $0.95–$0.99 (a rational trade if the event is truly unlikely). But the bot is entering at $0.991–$1.000 — likely because by the time the copy-trade executes, the price has moved against it due to the whale's own purchase and/or slippage.

### By Whale Score Tier

| Tier | Open | Avg Unrealized | |
|---|---|---|---|
| 85+ | 4 | -$25.00 | Largest single loss ($100 on Chris Murphy) |
| 65–75 | 4 | -$40.01 | Two $55 Bitcoin positions |
| 55–65 | 12 | -$25.00 avg | Consistent ~50% losses |

**No tier is outperforming.** The 85+ tier includes the worst position (Chris Murphy at -$100). With n=20 and a systematic entry-price bug, tier differentiation is meaningless — the problem is universal.

---

## 4. Sizing & Bankroll Analysis

### Capital Deployment

| Metric | Value | Assessment |
|---|---|---|
| Total bankroll | $9,440.17 | -5.6% from peak |
| Capital deployed | $1,790.00 | 19.0% utilization |
| Unrealized P&L | -$559.83 | -31.3% return on deployed |
| Average position size | $89.50 | Small but appropriate at ~0.9% of bankroll |
| Largest position | $200.00 (implied: Chris Murphy) | 2.1% of bankroll |

**Utilization at 19% is healthy** for a strategy in its first 24 hours. The problem isn't over-deployment; it's deploying into mathematically losing trades.

### Position Sizing Logic

The tier-based sizing is producing:
- 85+ tier: ~$100–$200 positions (1–2% of bankroll) 
- 65–75 tier: ~$55–$100 positions
- 55–65 tier: ~$25 positions (0.25–0.5% of bankroll)

**These sizes are reasonable in isolation.** The confidence multiplier appears to be working. However, the sizing formula is irrelevant when every trade has negative expected value by construction (entering at ≥0.991).

### Drawdown

5.6% drawdown is within the 15% circuit breaker. If the PRICE_RANGE bug continues and all positions move to 0.500, unrealized loss would reach ~$895, putting drawdown at ~9%. If markets move to 0.00 (which positions entered at 1.000 can do), losses reach $1,790 and drawdown hits 17.9% — breaching the circuit breaker. **The circuit breaker should have a tighter unrealized-loss trigger (10%) given how toxic these positions are.**

---

## 5. Strategy-Level Improvements

### A. Fix the Price Gate (Critical)
As detailed above. Without this, nothing else matters.

### B. Implement Exit Strategy (High Priority)
The bot has **no exit mechanism** other than market resolution. With positions in 2026–2028 markets, capital will be locked for years. Needed:
- **Take-profit exits:** If a position reaches +X% unrealized gain, sell. Given the corrected price range (0.05–0.70), a reasonable take-profit would be when the token appreciates 15–25%.
- **Stop-loss exits:** If a position drops below -20% unrealized, exit. The current portfolio shows 15 of 20 positions at exactly -50%, meaning there was no mechanism to cut losses.
- **Time-based exits:** If a position hasn't moved favorably within 48–72 hours, exit at market price and redeploy.

### C. Add Maximum Time-to-Resolution Gate (High Priority)
The current system has a minimum of 6 hours but effectively no maximum. The bot is entering 2028 presidential nomination markets. **Add a MAX_TIME_TO_RESOLUTION of 90 days.** This caps opportunity cost and keeps capital rotating. Markets 2+ years from resolution are:
- More likely to see adverse price movements
- More susceptible to fee drag and liquidity events
- Lower information advantage for whale signals (whales are good at near-term timing, not 3-year predictions)

### D. Add Latency/Slippage Awareness (Medium Priority)
The entry prices at 0.991–1.000 when the whale was likely buying at 0.93–0.97 suggests the copy trade executes too late and pays slippage. The bot needs:
- **Maximum slippage from whale's fill price:** Reject if the current ask is >2% worse than the whale's execution price.
- **Speed optimization:** Reduce signal-to-execution latency.

### E. Distinguish YES vs. NO Token Buys (Medium Priority)
The TRADE_IS_BUY gate treats YES buys and NO buys identically. These have very different characteristics:
- Buying YES at 0.15 = "I think this unlikely thing will happen" (contrarian, higher edge if correct)
- Buying NO at 0.95 = "I think this likely thing won't NOT happen" (consensus, low edge, high risk of ruin per dollar)

**Whale NO buys near 1.00 should be excluded or require a much higher whale score (≥85).** The asymmetric risk/reward is atrocious.

### F. De-duplicate Market Exposure
Multiple positions in the same market (Rachida Dati ×2, Bitcoin $35k ×2, Danish election ×2) indicate POSITION_CAP isn't functioning. Fix the cap and add a cooldown period (minimum 1 hour between entries in the same market).

---

## 6. Priority Action List

### #1: Fix PRICE_RANGE Gate Logic (Expected Impact: Existential)
- **What:** Inspect and correct the PRICE_RANGE conditional. It is almost certainly inverted.
- **Change to:** `PASS if 0.05 <= entry_execution_price <= 0.70` — verify the variable being checked is the actual price the bot will pay, not a complementary token price or market mid.
- **Why:** Every single open position violates the intended price range. The entire -$559.83 unrealized loss traces to this bug. Nothing else matters until this is fixed.
- **Confidence:** Very high. The evidence is unambiguous — 20/20 positions at prices outside the stated range.

### #2: Add Maximum Time-to-Resolution Gate (Expected Impact: High)
- **What:** Add `MAX_TIME_TO_RESOLUTION = 90 days` as a hard gate.
- **Value:** 90 days (roughly one quarter). Can be tuned tighter to 30–60 days after collecting performance data.
- **Why:** The bot is entering 2026–2028 markets where capital is locked indefinitely. Whale edge decays rapidly with time horizon; a whale buying NO on "Jon Stewart 2028" doesn't have information you don't have. Short-dated markets are where copy-trading signal has the most value.
- **Confidence:** High (structural argument, doesn't require performance data).

### #3: Fix POSITION_CAP Gate and De-duplicate (Expected Impact: Medium-High)
- **What:** Debug why POSITION_CAP shows 0 rejections despite duplicate positions in the same market. Ensure market identity matching works (exact market ID, not string matching). Add a 1-hour cooldown between entries in the same market.
- **Value:** Cap at 3% of bankroll per market (tighter than current 5%) since concentration killed the portfolio here.
- **Why:** Duplicate positions double loss exposure with no diversification benefit. The 12,636 "executed" signals vs. 20 open positions also suggests a counting bug that may be related.
- **Confidence:** Medium-high. The duplicates are visible in the data but the root cause requires code inspection.

### #4: Implement Stop-Loss Exit at -15% Unrealized (Expected Impact: Medium)
- **What:** Add a position monitor that sells any position whose unrealized P&L drops below -15%.
- **Value:** -15% stop-loss per position (translates to ~-0.15% of bankroll on a typical $100 position).
- **Why:** 15 of 20 positions are at -50% unrealized. A -