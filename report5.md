# Whale Bot Analysis Report
Generated: 2026-03-19 23:46 UTC

# Whale Bot Diagnostic Report

---

## 1. Executive Summary

**This strategy is completely non-functional.** Zero trades were executed from 34,243 evaluated signals — a 0.00% pass rate. The gates are stacked so aggressively that the compound rejection rate is 100%. The single most important thing to fix before the next run is to understand why "Whitelisted whale wallets" is **zero** — this suggests either a broken configuration or an ingestion pipeline that is evaluating undifferentiated market noise rather than curated whale signals — and then to widen or reorder the gates so that at minimum a few dozen signals per day survive the funnel.

---

## 2. Signal Funnel Diagnosis

### Reconstructing the Sequential Funnel

Since gates short-circuit, we can reconstruct exactly how many signals reached each stage:

| Gate | Rejected | Reached | Passed | Kill Rate at Gate |
|---|---|---|---|---|
| TRADE_IS_BUY | 23,250 | 34,243 | 10,993 | 67.9% |
| *(TRADE_SIZE_MIN)* | 0 | 10,993 | 10,993 | 0.0% |
| PRICE_RANGE | 7,920 | 10,993 | 3,073 | 72.1% |
| PRICE_ASSERTION_FAILED | 2,487 | 3,073 | 586 | 80.9% |
| MAX_TIME_TO_RESOLUTION | 585 | 586 | 1 | 99.8% |
| TIME_TO_RESOLUTION | 1 | 1 | 0 | 100% |
| WHALE_SCORE_MIN | — | 0 | — | never reached |
| MARKET_OI_MIN | — | 0 | — | never reached |
| ORDERBOOK_DEPTH | — | 0 | — | never reached |

**Compound pass rate: 0 / 34,243 = 0.000%**. Even a 1% per-gate leakage improvement at each stage would still produce nearly zero trades because the bottleneck is multiplicative. Let me assess each gate:

---

### Gate 1: TRADE_IS_BUY — 67.9% rejection
**Verdict: Functioning as designed, but costly.**

Prediction market order flow is roughly symmetric. Filtering out all sells is a legitimate design choice (buying tokens is simpler to reason about for copy-trading), but eliminating 2/3 of the signal universe is a steep tax. This is **acceptable** for now — the real problems are downstream.

However, note that a whale aggressively **selling** a YES token at $0.85 (effectively saying "this won't happen") is an equally informative signal. You're leaving half the alpha on the table.

---

### Gate 2: TRADE_SIZE_MIN ($500) — 0 rejections
**Verdict: Suspicious — possibly misconfigured or redundant.**

Zero rejections means every single buy signal was already ≥$500. There are two explanations:
- **(a)** The upstream signal feed is pre-filtered to trades ≥$500 already, making this gate dead code.
- **(b)** The signal source is exclusively whale-level activity where sub-$500 trades don't exist.

Either way, this gate does nothing. Not a problem per se, but it reveals that the signal feed may already be pre-filtered in ways that aren't visible here.

**Recommendation:** Verify the signal source. If it's already pre-filtered, remove this gate to simplify debugging. If not, the $500 threshold is reasonable.

---

### Gate 3: PRICE_RANGE (0.05–0.70) — 72.1% kill rate on remaining signals
**Verdict: TOO AGGRESSIVE. This is the first major bottleneck.**

Of 10,993 buy signals, 7,920 (72%) have token prices outside the 0.05–0.70 range. This is an enormous rejection rate. The most likely breakdown:

- **Above 0.70:** Whales buying tokens priced 0.70–0.95. These are "high-conviction, near-certainty" trades — a whale buying a YES token at $0.82 is saying "I'm 82%+ confident this resolves YES and the market is underpriced." The 0.70 cap discards these entirely. While margins are thinner, these trades often have **higher hit rates**.
- **Below 0.05:** Long-shot bets. The 0.05 floor is sensible — these are lottery tickets with terrible expected value for copy-trading.

**Specific recommendation:** Widen the upper bound from **0.70 → 0.85**. This captures high-conviction whale plays while still avoiding the 0.90+ range where the edge-to-slippage ratio collapses. Conservatively, this should recover ~40-50% of the 7,920 rejected signals (roughly +3,000–4,000 signals entering the next stage).

Keep the 0.05 lower bound — possibly raise it to 0.08 for additional noise reduction, but this is a secondary concern.

---

### Gate 4: PRICE_ASSERTION_FAILED — 80.9% kill rate (2,487 / 3,073)
**Verdict: CRITICAL PROBLEM. This is a phantom gate not in the system description.**

This gate name does **not appear** in the documented gate list. This is either:
1. **A staleness/slippage check** — the bot detects that the market price has moved since the whale's trade was observed, and the copy entry price would be materially different.
2. **An internal validation error** — a bug where the price data doesn't match expected format/range.
3. **A redundant price check** — e.g., the price after the whale's trade now falls outside 0.05–0.70, even though the whale's entry was inside range.

An 80.9% failure rate is **catastrophically high** and suggests one of two problems:
- **Signal latency:** By the time the bot evaluates a whale's trade, the market has already moved. This is common in copy-trading — other bots or market participants front-run the signal.
- **Logic bug:** The assertion is checking something incorrectly.

**Specific recommendation:** Add logging to capture the exact assertion condition and the delta between whale entry price and current market price for every failure. If latency is the root cause, the fix is either faster ingestion (sub-second) or a price tolerance band (e.g., accept if current price is within ±0.05 of whale entry). If it's a bug, fix the bug.

**This gate alone may be responsible for the entire system failure.** Even if all other gates were perfect, an 81% kill rate at this stage makes the strategy non-viable.

---

### Gate 5: MAX_TIME_TO_RESOLUTION — 99.8% kill rate (585 / 586)
**Verdict: DEVASTATING. This is the second fatal bottleneck.**

Of 586 signals that survive all prior gates, 585 fail the time-to-resolution check (≥6 hours). This means nearly every qualifying signal is on a market that resolves within 6 hours.

This is not surprising: short-duration markets generate the most whale activity because that's where the information edge is most time-sensitive. A whale trading on a market 2 hours before resolution likely has **strong directional conviction** — and you're systematically excluding these signals.

The 6-hour minimum was presumably designed to avoid being caught in end-of-life liquidity/volatility. But ≥6 hours is far too conservative.

**Specific recommendation:** Reduce from **6 hours → 1 hour**. A 1-hour buffer is sufficient to avoid literal resolution-window chaos while capturing the most information-rich trades. Alternatively, set it to 2 hours as a compromise. This alone should recover ~90%+ of the 585 rejected signals.

---

### Gates 6–9: WHALE_SCORE_MIN, MARKET_OI_MIN, ORDERBOOK_DEPTH, POSITION_CAP, CIRCUIT_BREAKER
**Verdict: NEVER REACHED. Completely untested.**

Zero signals have ever reached these gates. Their thresholds could be set to anything — we have no data on their impact. The WHALE_SCORE_MIN gate (≥55) and MARKET_OI_MIN (≥$50,000) are both likely to cause additional rejection once signals start flowing. Prepare for a second round of tuning.

---

### The Whitelisted Wallets Problem

**"Whitelisted whale wallets: 0" is a critical configuration gap.** If no wallets are whitelisted:
- The bot is likely evaluating **all** market activity, not whale-specific signals.
- This means the 34,243 signals include noise from retail traders, bots, and arbitrageurs.
- The WHALE_SCORE_MIN gate (never reached) would presumably filter these, but it never gets a chance to act.

**This must be resolved.** Either populate the whitelist with known profitable wallets (start with 10–20 addresses with verified track records), or confirm that the whale scoring pipeline is computing scores dynamically for all addresses and the whitelist is truly unnecessary.

---

## 3. Position Quality Analysis

**There is no data to analyze.** Zero positions opened, zero closed. No win rate, no P&L distribution, no tier analysis.

What I **can** infer from the funnel structure:

- The 586 signals that passed through to the time-to-resolution gate represent the "almost qualified" trades. These were buy-side, properly-sized, in the 0.05–0.70 price range, and passed the price assertion. **These 586 signals are the closest thing we have to a candidate pool** — their outcomes (what happened in those markets) should be retroactively tracked to estimate what the strategy's hit rate *would have been*.

**Recommendation:** Implement shadow-tracking. For every signal that passes through gate N but fails gate N+1, log the market outcome. This gives you a counterfactual: "if we had taken these trades, what would the P&L have been?" This is the single most valuable diagnostic you can build right now.

---

## 4. Sizing & Bankroll Analysis

**No capital has been deployed. Bankroll utilization is 0.0%.**

The sizing formula itself is well-structured in theory:
- Tiered allocation (0.5%–2% of $10,000 = $50–$200 base size per trade) is conservative and appropriate for a simulation.
- The confidence multiplier (0.5x–1.5x) provides reasonable range.
- Max-per-market exposure cap at 5% ($500) prevents concentration.
- Rounding to $10 is a minor detail but fine.

**Potential issues once trades start flowing:**

1. **$50 minimum effective size (0.5% × $10K × 0.5 confidence):** On Polymarket, a $50 position generates negligible P&L. Even at a 70% win rate buying at $0.50, the expected profit per trade is $50 × 0.70 × ($1.00 - $0.50) - $50 × 0.30 × $0.50 = $17.50 - $7.50 = $10.00. Net of fees and slippage, this barely moves the needle. Consider raising the floor to $100 minimum copy size.

2. **Utilization target:** With 0.5%–2% sizing and a 5% per-market cap, you'd need 20+ uncorrelated open positions to deploy even 20% of bankroll. This is fine structurally but means the strategy scales slowly.

3. **The 15% drawdown circuit breaker** will never trigger because you'd need to lose $1,500 — that's 7.5–30 full position losses. This is a reasonable backstop.

---

## 5. Strategy-Level Improvements

### 5.1 — Incorporate SELL signals as SHORT-equivalent positions
Currently discarding 67.9% of signals. A whale selling YES tokens at $0.75 is expressing the same conviction as buying NO tokens at $0.25. Map sell signals to equivalent buy-NO positions. This roughly doubles your signal universe without adding complexity.

### 5.2 — Implement signal latency measurement
The PRICE_ASSERTION_FAILED gate (81% kill rate) almost certainly reflects a latency problem. Measure the time delta between whale trade timestamp and bot evaluation timestamp. If median latency exceeds 30 seconds, the signal feed architecture needs to be rebuilt with WebSocket streaming rather than polling.

### 5.3 — Add time-weighted exit strategy
There's no documented exit logic. Copy-trading entries without exit management is dangerous:
- **Time-based:** Exit at 50% of remaining time to resolution.
- **Profit-target:** Exit at 2x the entry-to-resolution spread (e.g., buy at $0.55, exit at $0.65 if resolution value is $1.00).
- **Stop-loss:** Exit if position loses >30% of entry value.
- **Copy-exit:** If the whale sells, you sell.

### 5.4 — Whale wallet discovery and scoring
With 0 whitelisted wallets, the system has no alpha source. Build a scoring pipeline that:
1. Identifies addresses with ≥50 historical trades on the platform
2. Computes rolling 30-day ROI and Sharpe ratio
3. Auto-whitelists addresses scoring ≥55 on the composite whale score
4. Re-evaluates weekly and removes underperformers

### 5.5 — Correlated market detection
Prediction markets on the same event (e.g., "Will X happen by June?" and "Will X happen by July?") create correlated exposure. Add a market-cluster check to avoid concentrating in effectively-the-same position across multiple markets.

---

## 6. Priority Action List

### #1 — Fix PRICE_ASSERTION_FAILED (Expected impact: HIGH)
- **What:** Add detailed logging to the price assertion gate. Determine whether this is a latency issue, a logic bug, or an overly strict validation.
- **Value:** If latency, add a ±$0.03 tolerance band around the whale's entry price. If bug, fix the underlying code.
- **Why:** This gate kills 81% of surviving signals. Reducing its rejection rate from 81% to even 40% would 4x the number of signals reaching downstream gates. **This is almost certainly the root cause of zero executions.**
- **Confidence:** High — an 81% assertion failure rate is abnormal by any standard.

### #2 — Reduce MAX_TIME_TO_RESOLUTION from 6 hours to 1 hour
- **What:** Change the minimum time-to-resolution parameter from 360 minutes to 60 minutes.
- **Value:** 60 minutes (1 hour).
- **Why:** 99.8% of signals that survive all prior gates fail this check. The vast majority of high-signal whale trades occur in the final hours before resolution, when information advantages are most acute. A 1-hour buffer prevents literal last-minute resolution chaos while capturing the signal-dense window.
- **Confidence:** High — the data is unambiguous that 6 hours is too restrictive.

### #3 — Widen PRICE_RANGE upper bound from 0.70 to 0.85
- **What:** Change the maximum entry price from $0.70 to $0.85.
- **Value:** New range: 0.05–0.85.
- **Why:** 72% of buy signals fail this gate. Many are likely in the 0.70–0.90 range, representing high-conviction whale buys. The 0.70 ceiling systematically excludes the highest-probability trades. An 0.85 cap still avoids the >0.90 zone where edge-to-cost ratios are poor.
- **Confidence:** Medium-high — we don't have the price distribution of rejected signals, but the 72% rejection rate strongly implies the upper bound is the binding constraint, not the lower bound.

### #4 — Populate whale wallet whitelist (or validate dynamic scoring)
- **What:** Either manually add 10–20 verified profitable Polymarket wallets to the whitelist, or confirm that the dynamic whale scoring system is actually computing scores and that WHALE_SCORE_MIN will not kill 90%+ of signals once reached.
- **Value:** Start with wallets that have ≥100 historical trades and ≥10% ROI over the past 90 days.
- **Why:** "Whitelisted whale wallets: 0" means the bot has no curated alpha source. Even if signals flow through the gates, the quality of those signals depends entirely on whether you're copying smart money or random noise.
- **Confidence:** Medium — we don't know if the scoring pipeline works independently of the whitelist, but this is a fundamental architecture question that must be answered.

### #5 — Implement shadow-tracking for rejected signals
- **What:** For every signal rejected at gates 4+ (i.e., it passed the basic buy/price/size filters), log the market ID, entry price, and eventual resolution. Compute hypothetical P&L.
- **Value:** Run for 48–72 hours before the next live tuning cycle.
- **Why:** With zero closed positions, we have zero information about whether the underlying signal *even has edge*. Shadow-tracking the near-miss trades gives us a counterfactual P&L to validate the entire strategy thesis before deploying capital. Without this, every tuning decision is guesswork.
- **Confidence:** High — this is pure diagnostic infrastructure with no downside.

---

## Summary Table

| Metric | Current | Target After Fixes |
|---|---|---|
| Signal pass rate | 0.000% | 0.1–0.5% (~35–170 candidates/day) |
| Trades executed / day | 0 | 5–20 |
| Capital utilization | 0% | 5–15% |
| Whitelisted wallets | 0 | 10–20 |
| Price range | 0.05–0.70 | 0.05–0.85 |
| Min time to resolution | 6 hours | 1 hour |
| PRICE_ASSERTION tolerance | unknown | ±$0.03 |

The strategy concept (copy high-performing wallets on Polymarket) is sound. The implementation has a compounding filter problem that produces a dead system. Fix items #1 and #2, and you should immediately begin generating live trade data — which is the prerequisite for every subsequent optimization.