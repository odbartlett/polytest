# Whale Bot Analysis Report
Generated: 2026-03-19 21:16 UTC

# Whale Bot 24-Hour Simulation — Diagnostic Report

---

## 1. Executive Summary

**This strategy is completely non-functional.** Zero trades were executed from 49,960 evaluated signals, meaning the gate pipeline is so restrictive that it filters 100% of the opportunity set. No conclusions about trade quality, sizing, or whale selection can be drawn because the system never deployed capital.

**The single most important fix:** eliminate or dramatically widen the `MAX_TIME_TO_RESOLUTION` gate, which is an undocumented filter that killed 731 of the final 732 surviving signals — a 99.86% rejection rate at the last meaningful stage. Until this is fixed, the bot is an expensive logger, not a trading system.

---

## 2. Signal Funnel Diagnosis

Let me reconstruct the cumulative funnel from the short-circuit rejection counts:

| Gate | Rejected | Survivors After | Cumul. Pass Rate | Verdict |
|------|----------|-----------------|------------------|---------|
| Raw signals | — | 49,960 | 100% | — |
| TRADE_IS_BUY | 29,226 | 20,734 | 41.5% | ✅ Expected |
| TRADE_SIZE_MIN ($500) | 0 | 20,734 | 41.5% | ⚠️ Suspiciously lenient |
| PRICE_RANGE (0.05–0.70) | 14,912 | 5,822 | 11.7% | 🔴 Too aggressive |
| PRICE_ASSERTION_FAILED | 2,936 | 2,886 | 5.8% | ⚠️ Unclear gate |
| TIME_TO_RESOLUTION (≥6h) | 2,154 | 732 | 1.5% | ⚠️ Moderate concern |
| MAX_TIME_TO_RESOLUTION | 731 | 1 | 0.002% | 🔴🔴 Fatal bottleneck |
| ORDERBOOK_DEPTH | 1 | 0 | 0.000% | 🔴 Killed the sole survivor |

### Gate-by-Gate Assessment:

**TRADE_IS_BUY — 58.5% rejected → ✅ Reasonable (but limiting)**
Roughly half of all trades are sells; this is structurally expected. Filtering to buys-only is a valid simplification for a v1 system (avoids shorting complexity). However, this means you are ignoring ~29K signals per day that could include profitable short-side copies. *No immediate change needed, but flag for v2.*

**TRADE_SIZE_MIN ($500) — 0 rejected → ⚠️ Suspiciously lenient**
Zero rejections means either (a) the data feed pre-filters to large trades, or (b) the gate is evaluated under a different label, or (c) every single buy signal happened to be ≥$500. On Polymarket, a substantial fraction of trades are <$500, so option (a) is most likely. **Action:** Verify this gate is actually being evaluated. If the feed already filters to ≥$500, you're fine. If not, this gate may be broken and letting noise through (though the downstream gates would catch it today since nothing passes anyway).

**PRICE_RANGE (0.05–0.70) — 14,912 rejected (71.9% of remaining buys) → 🔴 Too aggressive**
This is the second-largest absolute filter and removes nearly 3 in 4 buy signals. The upper bound of 0.70 is the problem. A price of 0.72 represents 72% implied probability — that is *not* a near-resolved market. Many of the highest-conviction whale trades occur in the 0.70–0.90 range, where whales are piling into outcomes they believe are underpriced despite already being favorites. 

- **Lower bound (0.05):** Reasonable. Tokens below $0.05 are often illiquid lottery tickets with wide spreads.
- **Upper bound (0.70):** Too restrictive. Raise to **0.85**. This captures high-conviction favorites while still excluding the 0.85–0.95 zone where edge is minimal and downside is asymmetric (pay 0.90 to make 0.10 per contract).

*Quantitative reasoning:* If price is uniformly distributed (rough approximation), expanding from [0.05, 0.70] to [0.05, 0.85] increases the eligible range from 0.65 to 0.80 — a ~23% increase in eligible signals. Given the skew of prediction market prices toward extremes, the real increase would likely be larger. I estimate this change alone would increase survivors at this gate from 5,822 to roughly **7,500–9,000**.

**PRICE_ASSERTION_FAILED — 2,936 rejected (50.4% of remaining) → ⚠️ Needs investigation**
This gate label doesn't map to any of the nine documented gates. It likely represents a staleness or slippage check — verifying that the current market price still matches the price at which the whale traded. A 50% rejection rate suggests either:
- The check is too tight (e.g., requiring <1% price deviation when 2–3% would be acceptable), OR
- There's meaningful latency between signal detection and evaluation, causing natural price drift.

**Recommendation:** Log the actual price delta for these rejections. If most are failing by a small margin (e.g., 1–3 cents), widen the tolerance to **±$0.03** or **±5% of the whale's entry price**. If the price has moved 10+ cents, the gate is correctly protecting you from stale signals.

**TIME_TO_RESOLUTION (≥6h) — 2,154 rejected (74.6% of remaining) → ⚠️ Moderate concern**
This means 2,154 signals were on markets resolving within 6 hours. That's a lot — it suggests the data feed includes many near-expiry markets where whales are making last-minute plays. The 6-hour minimum is defensible (you need time to exit if wrong), but consider whether **4 hours** might be sufficient, which would save some of these signals.

However, this gate is *not* the critical problem. Even if you passed all 2,154, they'd still face the MAX_TIME_TO_RESOLUTION killer downstream.

**MAX_TIME_TO_RESOLUTION — 731 rejected of 732 remaining (99.86%) → 🔴🔴 FATAL**
This is the single point of catastrophic failure. This gate is **not documented** in the system description, which is itself a red flag — it suggests it was added ad-hoc or inherited from a template. 

Of the 732 signals that survived every other gate, this filter killed all but one. The most likely explanation: the maximum is set to something absurdly tight, like **24–48 hours**, creating a valid resolution window of only 6–48 hours. Most prediction markets on Polymarket resolve weeks to months out; a 48-hour max would exclude nearly all of them.

**Recommendation:** Either:
- **Remove this gate entirely**, or
- Set it to **≥ 60 days** (1,440 hours). There's no strong theoretical reason to exclude long-dated markets from a copy-trading strategy — if a whale with a good track record buys, the time horizon is less relevant than their edge.

**ORDERBOOK_DEPTH — 1 rejected → 🔴 Killed the sole survivor**
Only one signal ever reached this gate, and it failed. This is statistically meaningless (n=1), but it's worth checking whether the depth threshold is calibrated to the copy size. If you're trying to execute a $100–200 copy trade and requiring, say, $5,000 of book depth, that's reasonable. If the threshold is $50K+ of depth, it's likely too aggressive for the small sizes this bot would trade.

---

## 3. Position Quality Analysis

**No analysis possible.** Zero positions were opened or closed. There is no data on entry price effectiveness, whale score tier performance, market type win rates, or any other quality metric.

This is the core problem: the bot cannot learn or improve because it never takes action. A simulation that generates zero trades in 24 hours with ~50K signals is a configuration failure, not a strategy that needs tuning.

**What I would look for once trades are flowing:**
- Win rate by whale score tier (55–70 vs. 70–85 vs. 85+) to validate the tiered sizing
- Win rate by entry price bucket ($0.10–0.30 vs. $0.30–0.50 vs. $0.50–0.70+) to refine PRICE_RANGE
- Win rate by time-to-resolution bucket to calibrate the min/max resolution gates
- Average slippage (whale entry price vs. bot fill price) to assess execution quality
- P&L per dollar deployed by market category (politics, sports, crypto, etc.)

---

## 4. Sizing & Bankroll Analysis

**Capital utilization: 0%.** The $10,000 bankroll is entirely idle. The sizing formula itself (0.5%–2% of bankroll × confidence multiplier) would produce positions of $25–$300, which seems reasonable for a $10K portfolio. But this is entirely theoretical since no positions were taken.

**Theoretical assessment of the sizing formula:**

| Whale Score | Tier % | Base Size | Conf. Mult Range | Effective Size Range |
|-------------|--------|-----------|-------------------|---------------------|
| 55–70 | 0.5% | $50 | 0.5–1.5× | $25–$75 |
| 70–85 | 1.0% | $100 | 0.5–1.5× | $50–$150 |
| 85+ | 2.0% | $200 | 0.5–1.5× | $100–$300 |

- **Floor concern:** The round-down-to-nearest-$10 rule means a $25 position becomes $20. At $20 per trade, even 50 concurrent positions only deploy $1,000 (10% utilization). For a strategy that should be running 10–30 positions, target utilization would be 15–40%.
- **Recommendation:** Once trades are flowing, consider raising tier percentages by 50% (to 0.75%, 1.5%, 3.0%) if observed win rates justify it. But this is premature — get trades flowing first.

---

## 5. Strategy-Level Improvements

### 5a. The Undocumented Gate Problem
The existence of `MAX_TIME_TO_RESOLUTION` as an undocumented gate that kills 99.86% of late-stage signals is a **process failure**, not just a parameter failure. Every gate should be documented, and every new gate should require explicit justification for its threshold. **Recommendation:** Audit the codebase for any other unlisted filters or hardcoded thresholds that might be silently rejecting signals.

### 5b. Missing Whale Whitelist
The report shows **0 whitelisted whale wallets**. This is potentially another reason for zero trades — if a whitelist gate exists upstream (before these gates) or if the system requires explicit whitelisting before copying, no signals would ever pass. **Investigate immediately.** If the system is designed to only copy wallets on a whitelist, and the whitelist is empty, this is the actual root cause, and the funnel stats represent a fallback "evaluate everything" mode.

### 5c. Exit Strategy Gap
The gate system focuses entirely on *entry* criteria. There is no mention of:
- **Take-profit rules** (e.g., exit when price reaches 0.90)
- **Stop-loss rules** (e.g., exit when position is down 30%)
- **Time-based exits** (e.g., exit 2 hours before resolution)
- **Whale-exit mirroring** (exit when the copied whale sells)

Without exit rules, even perfect entries will bleed. **Priority for v2:** implement at minimum (a) copy the whale's exit, and (b) a time-based exit at T-2 hours before resolution.

### 5d. Sell-Side Consideration
Filtering to BUY-only discards 58.5% of all signals. On prediction markets, selling an overpriced YES token is economically identical to buying the underpriced NO token. If a high-scoring whale sells YES at $0.75 (implying they think the true probability is <75%), that's a strong signal. Consider adding SELL-side copying in v2, perhaps initially with half the position size as a risk control.

### 5e. Whale Score Threshold
WHALE_SCORE_MIN of 55 is fairly inclusive (just above median on a 0–100 scale). However, since this gate doesn't appear in the rejection funnel at all, either (a) it's never reached because upstream gates catch everything first, or (b) all remaining signals at that stage happen to have scores ≥55. Once the pipeline is unclogged, this gate may start rejecting meaningful volume. Starting at 55 is fine — you can tighten later based on tier performance data.

---

## 6. Priority Action List

### 🔴 #1 — Fix or Remove MAX_TIME_TO_RESOLUTION
- **What:** Set `MAX_TIME_TO_RESOLUTION` to 90 days (2,160 hours), or remove it entirely.
- **Why:** This single undocumented gate kills 731 of 732 signals that pass every other check. It is the primary reason the bot has zero trades. Fixing this alone would likely produce ~1 trade per day (assuming the 1 surviving signal pattern scales), and combined with other fixes, likely 5–20+ trades/day.
- **Expected impact:** Transformative — takes the system from non-functional to functional.
- **Confidence:** Very high. The math is unambiguous.

### 🔴 #2 — Widen PRICE_RANGE Upper Bound to 0.85
- **What:** Change `MAX_ENTRY_PRICE` from 0.70 to 0.85.
- **Why:** The current bound removes 72% of buy signals. Prices between 0.70–0.85 are active, liquid markets where whales frequently express strong directional views. Estimated to increase gate survivors by 25–50%.
- **Expected impact:** High. Roughly 3,000–5,000 additional signals pass this gate.
- **Confidence:** High. The 0.70 threshold has no strong theoretical basis.

### 🟡 #3 — Investigate PRICE_ASSERTION_FAILED and Widen Tolerance
- **What:** Log the price deltas causing this rejection. If most failures are within $0.03, widen the allowed slippage tolerance to ±$0.04 or ±5% of whale entry price.
- **Why:** This gate rejects 50% of signals that reach it. Some staleness protection is needed, but the current calibration is likely too tight given signal processing latency.
- **Expected impact:** Medium-high. Could recover ~1,000–1,500 additional signals.
- **Confidence:** Medium. Need to see the actual distribution of price deltas to calibrate properly.

### 🟡 #4 — Verify Whale Whitelist Configuration
- **What:** Confirm whether `whitelisted whale wallets: 0` is intentional (open mode, copy any whale meeting score threshold) or a misconfiguration (whitelist required but empty, so nothing passes).
- **Why:** If the whitelist is a hard requirement and it's empty, *this* is the actual root cause and the funnel stats may be misleading. Even if it's not a blocking issue, an empty whitelist means you haven't curated your whale universe, which is foundational to the strategy.
- **Expected impact:** Potentially critical if it's a blocking misconfiguration; otherwise informational.
- **Confidence:** Cannot determine from available data — requires code inspection.

### 🟢 #5 — Reduce TIME_TO_RESOLUTION Minimum from 6 Hours to 4 Hours
- **What:** Change `MIN_TIME_TO_RESOLUTION` from 6 hours to 4 hours.
- **Why:** 2,154 signals (74.6% of remaining at that stage) are on markets resolving within 6 hours. Reducing to 4 hours recovers some of these while still providing adequate time for position management. Whale trades in the 4–6 hour window before resolution are often the highest-conviction, most-informed signals.
- **Expected impact:** Medium. Estimated to recover 500–800 additional signals.
- **Confidence:** Medium. This is a judgment call — shorter windows increase information quality but reduce time to react to adverse moves.

---

## Appendix: What I Cannot Conclude

With **zero executed trades**, I want to be explicit about what remains unknown:

- **Whether the whale scoring system has any predictive power** — completely untested
- **Whether the sizing formula produces appropriate risk levels** — no live exposure data
- **Whether the copy latency is acceptable** — no fills to measure slippage against
- **Whether the 5% per-market position cap is relevant** — never tested, may be too tight or too loose
- **Whether the 15% circuit breaker is well-calibrated** — no drawdown data
- **Optimal entry price range** — no win/loss data by price bucket

The immediate priority is unambiguous: **unclog the pipeline so the simulation can generate actual trade data.** Fixes #1 and #2 alone should produce a meaningful sample within the next 24-hour cycle. Only then can any real strategy analysis begin.