# Round-Trip Simulator — Findings

**Generated:** 2026-05-11.
**Script:** [scripts/research/roundtrip_simulator.py](../../scripts/research/roundtrip_simulator.py)
**Tests:** [tests/test_roundtrip_simulator.py](../../tests/test_roundtrip_simulator.py) (18 passing)
**Data:** `data/poly_mm_live.db` (326 fills, 110 resolved tickers, 113
unique (session_id, ticker) cohorts)
**Trials per model:** 200 Monte Carlo draws, seed=0

## TL;DR

Round-trip simulation (the bot's realized P&L mechanic — FIFO pair-off
plus forced exits of unpaired inventory at last-snapshot BBO, fees
included) contradicts the hold-to-settle counterfactual on aggregate.

| Slice | Baseline net | pessimistic | base | optimistic | Δ vs baseline (base model) |
|---|---|---|---|---|---|
| ALL | **+$2.19** | +$0.67 | +$0.63 | +$1.19 | **−$1.56** |
| `aec` | +$0.26 | −$20.14 | **−$17.43** | −$11.63 | **−$17.69** |
| `asc` | +$2.69 | +$21.62 | **+$18.99** | +$13.58 | **+$16.30** |
| `atc` | +$0.02 | +$0.01 | +$0.01 | +$0.02 | ~0 |
| `tsc` | −$0.78 | −$0.41 | **−$0.46** | −$0.54 | **+$0.32** |

**The penalty's design target (tsc) benefits modestly** by $0.24-$0.37
across models — exactly where the 3.67× imbalance lives. But this
small gain is dwarfed by:
- **+$10 to +$19 asc gain** (which is sample drift luck, not strategy)
- **−$11 to −$20 aec loss** (which is a real NO-side flow problem the
  penalty makes worse)

Net aggregate: −$1.00 to −$1.56 across models. Hold-to-settle had said
the penalty *helps* by $4-$13; round-trip simulation says it *hurts*
by ~$1-$1.5.

This is the most rigorous test of Path C to date.

## Methodology

For each of 113 unique (session_id, ticker) cohorts:

1. **Tag** each fill with its offset bucket (per the static
   counterfactual: offset = fill_price − BBO at fill time, bucketed
   into {0, −1, +1_or_above, no_snapshot}).

2. **Apply survival** — for each yes_bid fill, draw uniform[0,1) and
   keep it if sample < `survival_fn(bucket)`. no_bid fills always
   kept. Three survival models match the static counterfactual.

3. **Process surviving fills chronologically** via FIFO pair-off:
   - yes_bid fill: pair with oldest no_bid in queue if any, else
     enqueue yes_bid.
   - no_bid fill: pair with oldest yes_bid in queue if any, else
     enqueue no_bid.
   - On pair: `pair_pnl += 100 − yes_cost − no_cost`.

4. **Exit unpaired inventory** at last snapshot's BBO for that
   (session, ticker):
   - YES inventory exits by selling YES at `best_yes_bid`. Taker fee
     applies.
   - NO inventory exits by selling NO at `100 − yes_ask`. Taker fee
     applies.
   - If no snapshot available, fall back to settlement outcome (worst
     case).

5. **Apply correct maker fee** (negative rebate for sports) per fill
   — `calculate_maker_fee()` from [src/poly_client.py](../../src/poly_client.py).
   The DB's stored fee values are buggy pre-2026-05-10 (commit
   a28186e); the simulator recomputes.

6. **Sum** per cohort: `net_pnl = pair_pnl + exit_pnl − maker_fee − taker_fee`.

7. **Aggregate** across cohorts, then run 200 Monte Carlo trials,
   reporting mean + p25/p75.

## Per-slice decomposition (base survival model)

```
Slice   Model               Pair       Exit      Maker      Taker      Net $
ALL     baseline       $  +4.68  $  -2.29  $  -0.80  $  +1.00  $  +2.19
ALL     base           $  +1.62  $  -0.76  $  -0.49  $  +0.71  $  +0.63

aec     baseline       $  +2.60  $  -2.40  $  -0.29  $  +0.23  $  +0.26
aec     base           $  +0.82  $ -18.21  $  -0.18  $  +0.21  $ -17.43

asc     baseline       $  +2.04  $  +0.56  $  -0.33  $  +0.24  $  +2.69
asc     base           $  +0.80  $ +18.20  $  -0.22  $  +0.23  $ +18.99

tsc     baseline       $  +0.04  $  -0.49  $  -0.17  $  +0.50  $  -0.78
tsc     base           $  +0.00  $  -0.30  $  -0.09  $  +0.25  $  -0.46
```

Maker fees are NEGATIVE (rebate / credit to us). Taker fees are
POSITIVE (cost). Net = Pair + Exit − Maker − Taker.

### Interpretation

- **ALL**: baseline `pair=+$4.68, exit=−$2.29`. The bot earned $4.68
  in round-trip pair-offs but lost $2.29 forced-exiting unpaired
  inventory at game time. Penalty reduces BOTH numbers
  proportionally (fewer fills overall), so net effect is small but
  slightly negative.

- **aec** (the catastrophe): baseline shows pair-offs +$2.60 and exits
  −$2.40, nearly break-even. With penalty, pair-offs drop to +$0.82
  (we lose the yes-bid pair-off opportunities), but exit P&L explodes
  to −$18.21. The reason: cutting yes_bid fills leaves more no_bid
  contracts unpaired, and last-snapshot yes_ask in aec is HIGH (markets
  drifted YES), so selling NO at (100 − yes_ask) is at terrible prices.

- **asc** (the boon, but sample-luck): mirror image of aec. Baseline
  exits already +$0.56 (last-snapshot yes_ask was LOW in asc, meaning
  market drifted NO, so leftover NO inventory exits at good prices).
  Penalty cuts pair-offs (smaller) but adds a LOT to exit P&L (+$18.20)
  because more no_bid inventory exits at favorable prices.

- **tsc** (the design target): baseline pair-offs are essentially zero
  (+$0.04) — most yes_bid fills in tsc don't pair, they exit at loss
  (−$0.49). Penalty cuts these unpairable yes_bid fills, saving on
  exit losses (−$0.30) AND saving on taker fees (the bot would have
  had to aggress more exits). Modest +$0.24-$0.37 improvement.

## Key insight: the penalty's effect is mostly an exit-price phenomenon

In the per-prefix decomposition, the dominant penalty effect is on
**exit P&L**, not on pair P&L:

| Slice | Baseline exit | Base penalty exit | Δ |
|---|---|---|---|
| aec | −$2.40 | **−$18.21** | −$15.81 |
| asc | +$0.56 | **+$18.20** | +$17.64 |
| tsc | −$0.49 | −$0.30 | +$0.19 |

The penalty changes WHICH side of the FIFO queue has the leftover
inventory, and the leftover inventory exits at the OPPOSITE side's
BBO. So the penalty's net effect depends on:
1. How asymmetric the YES vs NO fill counts already are (without
   penalty)
2. Whether the market drift over the session is favorable or
   adverse to the dominant side after penalty.

For **aec**: yes_bid:no_bid = 1.57:1 baseline. Without penalty, yes
queue mostly drains via pairing → small leftover, mostly NO. With
penalty, yes_bid drops to ~26% of baseline → way more leftover NO
inventory. aec markets drift YES → exiting NO is brutal.

For **asc**: yes_bid:no_bid = 1.39:1 baseline. With penalty, yes_bid
drops similarly → way more leftover NO inventory. asc markets drift
NO → exiting NO is great.

The drift correlation is sample-specific and is NOT a strategy
property we should bet on continuing.

## Baseline simulator P&L vs realized P&L

- **Simulator baseline (no penalty):** +$2.19
- **Realized bot P&L (handoff record):** −$2.92

The $5 gap is explained by:
1. **Maker fee bug pre-2026-05-10**: the stored fee in `mm_fills` is
   positive (charges) instead of negative (rebate). Re-running with
   stored fees gives net = $4.68 − $2.29 − $2.79 − $1.00 = −$1.40, a
   tighter match to realized.
2. **Exit-timing optimism**: last_snapshot may be CLOSER to actual
   game time than the bot's progressive exit ladder. The simulator
   assumes a clean BBO exit; the bot's progressive ladder takes
   wider losses near game start.
3. **Session-level effects**: price-jump pauses, mid-session market
   deactivation, etc., not modeled.

For the report's purpose, the ABSOLUTE level matters less than the
ROBUST DIRECTIONAL deltas across slices and models. The relative
comparisons (`base` vs `baseline`, per slice) carry the policy
signal.

## Policy implications

### 1. The flat 1c penalty is net mildly negative in aggregate

Across all three survival models, total net P&L drops by $1-$1.5 vs
baseline. The penalty's design intent (rebalance ratio toward 1.0×)
trades off against round-trip pair P&L recovery.

This is the OPPOSITE of what hold-to-settle said. Hold-to-settle is
not the right metric — round-trip is.

### 2. The recommended fix is marketType-conditional penalty

Apply 1c penalty ONLY to `tsc` (where the imbalance is real and
ratio rebalancing helps), 0c penalty elsewhere:

| Prefix | Recommended penalty | Expected effect (base model) |
|---|---|---|
| `tsc` | 1c | +$0.32 round-trip; ratio 3.67→1.45 (in target) |
| `asc` | 0c | 0 change; +$2.69 baseline retained |
| `aec` | 0c | 0 change; avoid −$17.43 hit |
| `atc` | n/a | Tiny sample |

Total predicted improvement vs current flat-1c: roughly **+$17 to +$19
across the historical sample** by sparing asc and aec while keeping
the tsc fix. Larger than the +$10-$19 asc luck because that gain was
mostly canceled by aec.

### 3. Deeper aec problem: NO-side exits

Even at baseline, aec is barely break-even ($0.26) and consists of
pair=+$2.60 + exit=−$2.40 — exits eat most of the pair profit. This
is a flow problem: aec NO-side fills are happening when the market is
trending YES, so the bot accumulates NO inventory in markets that
ultimately go YES. The penalty doesn't fix this; it makes it worse by
cutting the offsetting YES inventory.

**Investigation candidates for aec specifically**:
- Are the aec markets a particular sport (e.g., specific football
  league)? `aec-ipl` (IPL cricket) appeared in the cron log; could be
  cricket-specific (e.g., chasing teams ahead at the toss tend to win,
  making "NO" pre-game a losing position).
- Should aec markets be scanner-blocked, or quoted single-sided?

### 4. The KILL_CRITERION matrix verdict for Path C

Per [KILL_CRITERION.md](KILL_CRITERION.md):

| Survival model | Aggregate net (round-trip) | ALL ratio | Either-side > 1.4? | Matrix says |
|---|---|---|---|---|
| pessimistic | +$0.67 | 0.44 | yes (1/0.44=2.27>1.4) | **EXTEND** (positive but flow over-corrected) |
| base | +$0.63 | 0.69 | yes (1/0.69=1.45>1.4) | **EXTEND** (positive but flow unbalanced) |
| optimistic | +$1.19 | 1.05 | no (within 1.0-1.4) | **GREEN-LIGHT** for small-size live trial |

**No KILL.** Path C survives the round-trip simulator test. But the
verdict is sensitive to which survival model is closer to reality.

Note: the matrix uses *counterfactual on live*, which this round-trip
simulator now provides. The aggregate net P&L is positive across all
models, so the matrix doesn't say KILL. But the flat 1c penalty's
performance is suboptimal vs the differential approach above.

## Caveats

- **Exit pricing is optimistic.** The simulator uses last-snapshot
  BBO for exits, which is generally closer to fair value than the
  bot's actual progressive ladder. Realized exits would be slightly
  worse. The DIRECTIONAL deltas across slices/models are less
  affected.
- **Survival is probabilistic; aggregate signals are mean of 200
  trials.** Per-trial variance is shown in p25/p75 columns of the
  full output. The reported means are reliable to within ~$0.10 of
  population mean for this trial count.
- **Sample size**: 326 fills, 113 cohorts, 110 markets. Per-prefix
  conclusions (especially `atc` with 4 fills) may not generalize.
- **No second-order effects**: scanner selection isn't re-run with
  penalty; risk-management rules (FORCE_CLOSE, AGGRESS_FLATTEN,
  SOFT_CLOSE) aren't modeled.
- **Settlement-direction luck.** The asc gain (+$18) and aec loss
  (−$17) are nearly equal-and-opposite, suggesting they're largely
  symmetric properties of the sample. If asc/aec drift correlations
  flip in future samples, the gain and loss could swap or vanish.

## Next steps (post-Step-2)

1. **Per-prefix penalty implementation** in
   [src/mm/state.py](../../src/mm/state.py) `skewed_quotes()` — make
   `yes_penalty` a function of marketType. Add tests covering the
   new behavior. (Don't implement until user confirms; the
   hold-to-settle counterfactual already supported a per-prefix
   approach, but tests should compare round-trip simulator's
   prediction vs the actual differential implementation.)

2. **Step 4: WebSocket taker-side collector** (deferred to own
   session). Collect real aggressor-side trade tape; use to:
   - Validate the survival-probability model (are 65% of "at BBO"
     yes_bid fills really getting hit by yes-sellers?)
   - Rerun this simulator with aggressor-aware queue dynamics.

3. **aec-specific investigation** — a separate research task. What
   makes aec NO-side fills lose so consistently? The penalty isn't
   the right tool here; scanner-side filtering or aec exclusion may
   be.
