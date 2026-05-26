# Path B Re-Assessment

**Generated:** 2026-05-20.
**Supersedes ranking in:** [path_b_options.md](path_b_options.md) (kept for historical context).
**Inputs to this re-assessment:**
- [trade_tape_aggressor_findings.md](trade_tape_aggressor_findings.md) — flow direction has flipped per prefix
- [roundtrip_simulator_findings.md](roundtrip_simulator_findings.md) — best static config is +$2.48 (sample-specific)
- [simulator_recalibration_findings.md](simulator_recalibration_findings.md) — auto-calibration on trade tape is catastrophic retrospectively
- Fork A (commit 78613e8) — YES penalty reverted to 0; bot now at "baseline" config

## TL;DR — updated lean

| Rank | Option | Original priority | Updated priority | Reason |
|---|---|---|---|---|
| 1 | Same-platform taker (Option 3) | LOW (deferred) | **HIGH** | Trade tape provides the exact aggressor data needed. Counterfactual on historical fills + tape is the natural next experiment. |
| 2 | Single-sided MM (NEW Option 4) | — | MEDIUM | Defensive variant of Option 3; quote only the under-hit side per prefix. |
| 3 | Kalshi politics (Option 2) | MEDIUM | MEDIUM | Long-tail bet on a different market with different fee structure. |
| 4 | Market-type split / asc-only (Option 1) | **HIGH** (quick win) | **LOW** | The "asc balanced" historical signal is unreliable — current trade tape shows asc-nba flipped to NO-heavy. Same heterogeneity problem we found everywhere. |

The original ranking assumed prefix-level fill ratios were a stable property. The trade tape data falsified that assumption. **The option that benefits most from this falsification is Option 3 (taker), because it doesn't need a stable per-prefix signal — it adapts to whatever the current flow is.**

## Option 1 — Market-type split (DOWNGRADED)

### What changed

Original assessment relied on historical fill ratios:
- `tsc` 3.67× (worst)
- `asc` 1.39× (already in "balanced" window)
- `aec` 1.57×

The proposal was to restrict the bot to `asc` markets only.

Trade tape data (May 11-19, 1810 trades) shows:

| Prefix | Historical ratio | Current YES_BID share | Direction |
|---|---|---|---|
| `tsc-nba` | 3.33× yes-heavy | 19% (NO-heavy) | **FLIPPED** |
| `tsc-mlb` | 4.67× yes-heavy | 47% (balanced) | flipped (toward bal) |
| `asc-nba` | 1.34× yes-heavy | 44% (slightly NO) | mild flip |
| `aec-nhl` | 1.02× balanced | 49% (balanced) | consistent |

The hypothesis "`asc` is balanced because alt-spread markets attract professional flow" is now suspect — `asc-nba` itself has drifted to slightly-NO-heavy. The signal that motivated Option 1 (per-prefix flow stability) is the same signal the trade tape data falsifies.

### Verdict

**Downgrade to LOW priority.** Restricting the bot to `asc` is gambling on the historical ratio holding — and the recent data says it won't.

The work IS partially still useful: the scanner whitelist (commit 8fea806) excluded weather markets and codified the sport list. Adding a per-prefix filter to the scanner is one-line if we want it. But this is now a scoping/risk reduction tool, not a "quick win to positive P&L."

## Option 2 — Kalshi politics (UNCHANGED MEDIUM)

### What changed

The hypothesis was "politics markets have more symmetric flow than sports because longer time horizons attract two-sided participants."

What we learned doesn't directly speak to this hypothesis (the trade tape captures Polymarket sports, not Kalshi politics). But it adds a methodological caveat:

**Even if Kalshi politics flow LOOKS symmetric in initial data, the heterogeneity-by-subprefix pattern we saw on Polymarket might recur there.** A single "Kalshi politics yes:no ratio = 1.05" aggregate could be hiding 80%/20% flips at the individual market level.

### Verdict

**Keep at MEDIUM priority.** The Kalshi data collection plan (run `kalshi_daily_scan.py`, pull historical trades, compute per-market aggressor ratios) is still the right approach. Two changes to the plan:

1. **Compute per-market and per-category ratios from day 1.** Don't trust an aggregate; check for the heterogeneity we now know exists on Polymarket sports.
2. **Verify fee economics on real spreads, not theoretical maxima.** Kalshi maker fee is `+0.0175 × P(1-P) × 100` (positive cost) — even balanced flow needs sufficient spread. The Polymarket experience shows that paying maker fees instead of receiving rebates raises the breakeven significantly.

### Risk

This is a 2-3 week data-collection + analysis cycle before any trading commitment. Not an immediate move.

## Option 3 — Same-platform taker (UPGRADED to HIGH)

### What changed dramatically

The original blocker for Option 3 was: "we don't have aggressor data to validate the thesis that buy-YES flow on Polymarket is captureable by a taker."

**We now have that data.** The trade tape has 1,810 trades with full maker/taker side+intent. We can run the exact counterfactual the original plan called for, on real Polymarket data.

The thesis (from path_b_options.md):
> If YES-sellers consistently sell below fair, then a buyer who lifts their offer at 49c captures (fair - 49c) as a taker. The taker pays fees (~0.7c per contract at P=50), so taker edge ≈ +0.3c (positive but thin).

Now reinterpreted with current data:
- For prefixes where YES_BID share is LOW (= takers BUY YES = lifting YES_ASK below fair), being a taker on the BUY YES side is the captureable flow. e.g., `tsc-nba` 19% YES_BID share = 81% of trades had takers buying YES.
- For prefixes where YES_BID share is HIGH (= takers SELL YES = hitting YES_BID below fair), being a taker on the SELL YES side is the captureable flow. e.g., `astatc-mlb` 88% YES_BID share.

The directional persistence over a 7-day window per prefix is what makes this tractable.

### Decisive experiment (1-2 hours)

1. Take the 326 historical live fills as "the flow we were exposed to."
2. Pull the YES_ASK / YES_BID at each fill time (from `mm_snapshots`).
3. Compute the taker P&L if we'd lifted/hit at those prices instead of resting maker orders.
4. Account for taker fees (`0.02 × P(1-P) × 100` on Polymarket sports — much less than Kalshi's 7%).
5. Aggregate: was the taker counterfactual net positive after fees?

This is the "step 1" of the original Option 3 plan. We can do it NOW without any new infrastructure.

### What makes Option 3 more attractive post-revert

With YES penalty reverted, the simulator says realized P&L will stay near −$1.36. The MM strategy is structurally near break-even and we don't have a clear path to make it more positive (Path 1 exhausted).

Option 3 is the natural reframing: **STOP TRYING TO BE THE MAKER WHO GETS ADVERSELY SELECTED. BE THE TAKER WHO PROVIDES THE ADVERSE SELECTION (i.e., capture the same edge from the other side).**

The trade tape isn't just diagnostic — it's the data feed for a taker strategy. Real-time directional signal per market is exactly what a taker needs.

### Concrete next step

Build `scripts/research/taker_counterfactual.py`:

1. Reads `data/poly_mm_live.db` historical fills + `data/poly_trade_tape.db` (where overlap exists, which is minimal for historical) + `mm_snapshots` for orderbook state.
2. For each fill in mm_fills, computes "what if instead of waiting at maker price, we'd taken at the ASK price at that moment."
3. Tracks taker fee separately. Reports net P&L.
4. Reports per-prefix.
5. Compares vs the realized maker P&L.

If positive, Option 3 advances to: forward-looking taker observer that watches the trade tape in real-time and logs hypothetical lift opportunities. Then paper trade. Then live trial.

If negative, this is meaningful — it tells us the "fair value" our OBI was computing was indeed wrong (per the original Option 3 open question 1), and we have a fundamental issue with the bot's pricing model independent of taker vs maker role.

## Option 4 (NEW) — Single-sided MM

### What this is

Instead of always quoting BOTH YES and NO bids, quote ONLY the side where current trade tape says aggressor flow is LESS aggressive. The intuition:

- For `tsc-nba` (19% YES_BID share, 81% YES_ASK hit) — quote ONLY the YES side. Avoid the NO_BID side that's getting hit hard. Accept the YES_BID volume is low.
- For `astatc-mlb` (89% YES_BID share, 11% YES_ASK hit) — quote ONLY the NO side. Avoid the YES_BID side getting hit hard.

The trade tape determines the direction; the bot quotes one side per market. Inventory accumulates on the quoted side only.

### Why this is a "Path B" option

Current Path C is "quote both sides, try to capture spread, lose due to adverse selection." Single-sided MM is "quote only the side where you're NOT being adversely selected." It accepts lower volume in exchange for less adverse-selection cost.

### Pros vs Option 3

- Doesn't require taking (so no taker fees — keeps the maker rebate)
- Implementation is smaller than Option 3 (just a quote-skip filter)
- Inventory accumulates one-sided, which the existing inventory-cap risk management can handle

### Cons vs Option 3

- Likely lower edge than taker side (you're not capturing the directional flow, just avoiding the cost of being on the wrong side)
- Inventory accumulates without natural offset → exit risk is concentrated
- For some prefixes, quoting one side may produce so few fills that the strategy isn't worth running

### Decisive experiment

Same counterfactual mechanism as Option 3. For each historical fill, if the trade tape's per-prefix signal at that time would have suggested skipping that side, drop the fill. Compute net P&L. Compare to baseline.

**This is cheaper than Option 3** (no taker fee math, no aggressing dynamics). It's the natural "control" experiment for Option 3.

### Verdict

**MEDIUM priority.** Run alongside Option 3's counterfactual (same script, just one additional analysis path). If Option 3 is positive and Option 4 is also positive, choose Option 3 if the edge is big enough to overcome taker fees; choose Option 4 if not.

## Recommended next 2-3 sessions

1. **Run Option 3 counterfactual (1-2 hours).** Pure analysis, no infrastructure changes, no money at risk. Decisive for whether to commit to taker strategy.

2. **Run Option 4 counterfactual (within the same script).** Quick add-on.

3. **If both negative**: investigate whether OBI fair-value computation is itself wrong (the bot's edge thesis collapses if "fair" is biased). This is a deeper investigation.

4. **If Option 3 positive**: design forward observer (no real money). Watch trade tape live for a week, log hypothetical taker fills. Then escalate to paper or small-size live.

5. **If only Option 4 positive**: implement single-sided quoting (requires `src/mm/` changes, user approval) for the prefixes the trade tape flags. Smaller code change than Option 3.

## What this re-assessment doesn't say

- **Don't drop Path C entirely.** With the penalty reverted, Path C is now just "baseline MM" — that's our current operational state. The revert is the right move regardless of which Path B option we pursue.
- **Doesn't commit bankroll.** Capital allocation is still a separate decision.
- **Doesn't deprecate Option 1 or 2 entirely.** Both still have signal value (Option 1: scanner whitelist already in place; Option 2: Kalshi data collection can run in parallel). Just demoted from "next move" status.

## My recommendation: pursue Option 3 counterfactual first

Concrete: I'll build `scripts/research/taker_counterfactual.py` next session (or this one if you want to continue). The output answers "was the flow we were being adversely selected on captureable as a taker?" That's the highest-information experiment we can run with current data, and it gates everything else.

If you'd rather pursue Option 2 (Kalshi politics) or stay in Path C with baseline + monitoring, that's also defensible — but the data we have most clearly points at Option 3 as the next experiment worth running.
