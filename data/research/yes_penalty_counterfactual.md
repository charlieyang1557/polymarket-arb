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
