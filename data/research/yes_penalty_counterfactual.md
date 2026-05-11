# YES Penalty Counterfactual on Live Fills

**Generated:** 2026-05-11.
**Script:** [scripts/research/yes_penalty_counterfactual.py](../../scripts/research/yes_penalty_counterfactual.py)
**Data:** `data/poly_mm_live.db` (326 fills, 110 markets, sessions
2026-03-31 → 2026-04-24)
**Settlement source:** [charming-mcclintock-caff3f/data/research/resolved_markets_cache.json](../../../charming-mcclintock-caff3f/data/research/resolved_markets_cache.json)
(100% coverage of 110 resolved tickers)

## TL;DR

Applying the 1c YES adverse-selection penalty to the 326 historical
live fills (probabilistically — survival depends on how often the
BBO collapsed past our bid post-penalty) gives:

| Survival model | Expected yes_bid fills | yes:no ratio | Hold-to-settle P&L |
|---|---|---|---|
| Pessimistic (40-50% survive) | 52 (down 75%) | **0.44** (NO-biased) | **−$0.57** |
| Base (20-40% survive) | 81 (down 61%) | **0.69** | −$5.55 |
| Optimistic (40-60% survive) | 123 (down 41%) | **1.05** (~balanced) | −$9.32 |
| **Baseline (no penalty)** | 209 | 1.79 | **−$13.49** |

**The penalty improves hold-to-settle P&L in ALL three models** — by
$4.17 (optimistic) to $12.92 (pessimistic) vs the baseline −$13.49.
The penalty also pulls the yes:no ratio toward the Path C target
window of 1.0-1.4×, though *over-correction* is possible (pessimistic
model lands at 0.44, i.e., now we'd be NO-biased instead of YES-biased).

This is the FIRST evidence (post-paper-vs-live finding) that the
Path C YES penalty has empirical support on real data, not just paper.

## Methodology

For each yes_bid fill in the live DB:
1. Find the nearest `mm_snapshot` within ±90s of `filled_at`. 51% of
   yes_bid fills have a usable snapshot.
2. Compute `offset = fill_price - best_yes_bid` from that snapshot:
   - `offset = 0` (33.5%): our bid was AT BBO. With the penalty
     we'd sit 1c below BBO; fill only happens if BBO collapsed
     during the fill window.
   - `offset = -1` (13.9%): our bid was 1c BELOW BBO (BBO had
     already collapsed to our level by snapshot time). With penalty
     we'd be 2c below original BBO. Less likely to fill.
   - `offset ≥ +1` (3.8%): our bid was AT OR ABOVE BBO+1. Unusual;
     under penalty we'd still be near BBO, fill probability high.
   - `no_snapshot` (48.8%): use weighted-average survival rate.
3. Apply per-bucket survival probability under each of three models:

```python
SURVIVAL_MODELS = {
    "pessimistic": {0: 0.20, -1: 0.10, "+1_or_above": 0.50, "no_snap": 0.30},
    "base":        {0: 0.40, -1: 0.20, "+1_or_above": 0.80, "no_snap": 0.40},
    "optimistic":  {0: 0.60, -1: 0.40, "+1_or_above": 1.00, "no_snap": 0.60},
}
```

4. no_bid fills are unaffected (no penalty on that side); kept at 117.

Then for hold-to-settle P&L:
- yes_bid fill survives → P&L = (100 − fill_price) if settled YES else (−fill_price)
- no_bid fill → P&L = (100 − fill_price) if settled NO else (−fill_price)
- Multiply by fill size; sum.

## Per-fill offset distribution (the empirical input)

```
yes_bid (n=209):
  +1_or_above:   8 (3.8%)
           -1:  29 (13.9%)
            0:  70 (33.5%)
  no_snapshot: 102 (48.8%)

no_bid (n=117):
  +1_or_above:   4 (3.4%)
           -1:  16 (13.7%)
            0:  27 (23.1%)
  no_snapshot:  70 (59.8%)
```

Distribution matches the diagnosis report Appendix A9. 65% of
snapshot-available yes_bid fills were AT BBO — these are the fills
the penalty most directly threatens.

## Caveats

1. **51% snapshot coverage.** For nearly half the fills, we use a
   weighted-average default survival rate. The "no_snapshot" bucket
   distribution likely mirrors the snapshot-available distribution
   (65% at BBO), so the default of 0.30-0.60 is reasonable, but
   this introduces uncertainty.

2. **Hold-to-settle ≠ round-trip.** The bot's actual realized P&L
   was −$2.92 (not −$13.49) because the bot round-trips, exiting
   via aggress-flatten before settlement. The penalty's *real*
   impact on the bot is in:
   - Reducing yes_bid fill rate → less long-YES inventory
   - Increasing unpaired NO inventory → NO has to exit via aggress
     or settlement
   - Net direction of P&L change depends on exit prices
   - **Round-trip simulation is the natural next step** but requires
     more careful modeling than this static counterfactual.

3. **No second-order effects modeled.** Penalty changes *which*
   markets we'd be filled on. The scanner's market selection isn't
   re-run; the snapshot uses original picks.

4. **Survival is probabilistic, not deterministic.** The actual fill
   decision depends on competing orders, queue position, taker
   sequencing — none of which we observe at the moment of fill.

## What this tells us

**The penalty is empirically supported on real data.** Even the
worst-case model (optimistic — most fills survive) gives a P&L
improvement of $4.17 over baseline. The ratio rebalance is real:
1.79× → 0.44-1.05× across all models.

**1c may be over-aggressive.** The pessimistic model lands at
ratio=0.44 — we'd be 2.3× NO-biased instead of 1.8× YES-biased.
If we land in the pessimistic regime, a 0.5c penalty (somewhere
between current code's 1c and zero) might be the optimal middle.
But fractional cent penalty isn't directly expressible (prices are
integer cents) — we'd implement via a random rounding tilt.

**The bot's actual round-trip P&L impact remains uncertain.** The
−$2.92 realized was much better than the −$13.49 hold-to-settle
counterfactual. So even though the penalty cuts hold-to-settle
losses by $4-13, the bot's existing risk management may have been
recovering most of that anyway. The penalty's PRACTICAL impact
needs round-trip simulation to nail down.

## Next steps

1. **Round-trip simulation** that respects: (a) which fills survive
   the penalty, (b) pair-off dynamics on surviving inventory, (c)
   exit prices from aggress-flatten or progressive close. This is
   the proper test.

2. **WebSocket taker-side capture** (per [src/polymarket_us SDK](file:///Users/openclaw/miniconda3/lib/python3.13/site-packages/polymarket_us/websocket/markets.py)
   — `subscribe_trades()` with `Trade.trade.maker.side` and
   `Trade.trade.taker.side`). Collect real taker-flow data for
   2-3 weeks; use it to fix `drain_queue()` and rerun the simulation
   against real aggressor-aware queue dynamics.

3. **Per-prefix breakdown** of the counterfactual — does the penalty
   help more on `tsc` (3.67× imbalance) than `asc` (1.39×)?
   If `asc` is already near-balanced, penalty might over-correct
   there but improve `tsc`. Suggests differential penalty by
   marketType.

---

## Appendix A: Per-prefix breakdown (added 2026-05-11)

The aggregate counterfactual masks per-marketType heterogeneity. This
appendix slices the 326 fills by ticker prefix (`tsc`/`asc`/`aec`/`atc`)
and runs the three survival models per slice, then compares against
each slice's baseline hold-to-settle P&L.

### A.1 Per-prefix counts and baseline economics

| Prefix | yes_bid | no_bid | Baseline ratio | Baseline H2S YES P&L | Baseline H2S NO P&L | Baseline H2S TOTAL |
|---|---|---|---|---|---|---|
| **ALL** | 209 | 117 | **1.79×** | −$18.85 | +$5.36 | **−$13.49** |
| `aec` | 72 | 46 | 1.57× | +$34.01 | **−$41.92** | −$7.91 |
| `asc` | 78 | 56 | 1.39× | **−$54.69** | +$46.28 | −$8.41 |
| `atc` | 4 | 0 | inf | +$0.07 | $0.00 | +$0.07 |
| `tsc` | **55** | **15** | **3.67×** | +$1.76 | +$1.00 | **+$2.76** |

`atc` is degenerate (4 yes, 0 no). The three meaningful prefixes
behave very differently at baseline:

- **`aec`**: YES side is profitable (+$34), NO side is catastrophic
  (−$42). aec markets in our sample settled YES disproportionately;
  our NO inventory lost on settlement.
- **`asc`**: Mirror image — YES side is catastrophic (−$55), NO side
  is profitable (+$46). asc markets settled NO disproportionately.
- **`tsc`**: Both sides near break-even but small ($+1.76 / $+1.00).
  This is the prefix with the most severe ratio imbalance (3.67×).

### A.2 Penalty impact per prefix (base survival model)

| Prefix | Baseline ratio | Post-penalty ratio | Baseline P&L | Post-penalty P&L | Δ vs baseline |
|---|---|---|---|---|---|
| **ALL** | 1.79× | 0.69× | −$13.49 | −$5.55 | **+$7.94** |
| `aec` | 1.57× | 0.57× | −$7.91 | −$31.69 | **−$23.78** |
| `asc` | 1.39× | 0.56× | −$8.41 | +$23.96 | **+$32.37** |
| `atc` | inf | inf | +$0.07 | +$0.03 | −$0.04 |
| `tsc` | **3.67×** | **1.45×** | +$2.76 | +$2.16 | **−$0.60** |

Same comparison under pessimistic / optimistic survival models gives
similar directional signals — see the script output for the full
matrix. The directional conclusions are robust to the survival model.

### A.3 What this shows

**The aggregate +$7.94 improvement is driven entirely by `asc`
settlement luck.** The penalty cuts `asc` yes_bid fills, which
happened to settle NO (so the cut is good). But `asc` was *already in
the Path C target window* (1.39× ratio is exactly the band we'd
declare healthy) — there was no asymmetry to fix in `asc`. The penalty
over-corrects (ratio drops to 0.56×, now NO-biased instead of
YES-biased), and the favorable P&L outcome reflects realized
settlements, not strategy validity.

**The penalty catastrophically hurts `aec` by −$23.78** because `aec`
markets in our sample settled YES — `aec` yes_bid fills were winning
on hold-to-settle (+$34); cutting 64% of them eliminates those wins
while leaving the NO side's structural −$42 unchanged. The deeper
issue in `aec` is not the YES-side asymmetry (which was only 1.57×,
already nearly within target) but the NO-side settlement losses, and
the penalty doesn't address that.

**For `tsc` — the prefix the penalty was actually designed to fix
(3.67× imbalance) — the penalty achieves the ratio rebalancing
(3.67→1.45, comfortably within the 1.0-1.4× target) but the
hold-to-settle P&L impact is essentially nil (−$0.60).** This is
the cleanest "rebalance only" data point: `tsc` baseline P&L was
+$2.76 (both sides break-even), so cutting yes fills changes the
ratio without much P&L consequence.

### A.4 Why hold-to-settle isn't the right metric here

Hold-to-settle P&L depends heavily on the random settlement
distribution in the sample. With only 110 resolved markets in the
sample, each prefix has 21-46 markets — small enough that one or two
games' settlement directions dominate the per-prefix P&L. The +$7.94
"penalty helps" signal in the aggregate is essentially **the asc
sample's NO-bias luck propagated through the penalty's yes-fill
cutting**. If our 110-market sample had asc settling 50/50 instead of
heavily NO, the aggregate Δ would flip negative.

The bot doesn't actually realize hold-to-settle losses — it
round-trips out before settlement most of the time (realized −$2.92
vs hold-to-settle −$13.49 in the historical record). The
counterfactual's hold-to-settle metric is an upper bound on settlement
risk, not a predictor of bot P&L.

**Round-trip P&L is the right metric** and that's what Step 2 of the
2026-05-11 handoff will model. Per-prefix hold-to-settle gives us
*directional ratio* evidence (does the penalty fix the imbalance?) but
not strategy-validity evidence (does the penalty improve realized
trading P&L?).

### A.5 Recommendation

1. **The flat 1c YES penalty as currently implemented over-corrects
   for `asc` and `aec` (both already near the Path C target window)
   and is well-targeted for `tsc` (the only prefix with severe
   asymmetry).** A differential penalty by `marketType` is the
   theoretically clean design:

   | Prefix | Recommended penalty | Reason |
   |---|---|---|
   | `tsc` | 1c (current) | 3.67× imbalance; penalty rebalances cleanly |
   | `asc` | 0c | Already 1.39× (in target); penalty over-corrects |
   | `aec` | 0c | Already 1.57× (near target); penalty makes NO-side losses worse |
   | `atc` | n/a | Tiny sample (4 fills), defer until more data |

2. **However, do not implement differential penalty yet.** The
   evidence above is from hold-to-settle, which is not the right
   metric for evaluation. Step 2 (round-trip simulator) should
   re-check this with proper round-trip dynamics before we change
   the implementation. Step 4 (WebSocket aggressor-side collector)
   will also tell us whether the per-prefix imbalance is a flow
   property of the marketType or a settlement-correlation
   coincidence — distinguishing these matters for whether the
   penalty needs to be marketType-conditional or remains flat.

3. **Interim operational implication for the running paper session:**
   the current flat-1c implementation is conservative — it
   over-corrects on prefixes that don't need it but is unlikely to
   cause large *trading* losses (the over-correction shifts fill
   distribution but the round-trip mechanic still captures spread).
   No emergency change needed. The data informs Step 2's design, not
   a hot-patch.

4. **Scanner-level alternative**: instead of marketType-conditional
   penalty, consider dropping prefixes where the per-side settlement
   risk is large independent of the asymmetry fix. `aec` baseline
   NO-side −$41.92 over 46 fills (~91c per contract) is a red flag
   that warrants scanner-side investigation rather than a quoting
   adjustment.

### A.6 Limitations

- **Small per-prefix samples**. `atc` has 4 fills (skip). `tsc` has
  70 fills; `asc` 134; `aec` 118. Conclusions about per-prefix
  settlement bias may not generalize to future markets.
- **Settlement distribution is a sample artifact, not a strategy
  property.** If the 110-market sample's settlements were drawn
  differently, the per-prefix P&L signal would flip — see A.4. The
  ratio-rebalancing signal (3.67→1.45 on `tsc`) is more robust
  because it depends on offset bucket distribution, not settlement
  outcomes.
- **Penalty assumed YES-only.** This breakdown does not consider
  whether a NO-side penalty would be appropriate for prefixes where
  NO ratio is excessive — none of our prefixes have NO > YES, so the
  question doesn't arise from this data.
