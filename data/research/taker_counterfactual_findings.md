# Taker Counterfactual Findings — Path B Option 3

**Generated:** 2026-05-20.
**Script:** [scripts/research/taker_counterfactual.py](../../scripts/research/taker_counterfactual.py)
**Tests:** [tests/test_taker_counterfactual.py](../../tests/test_taker_counterfactual.py) (12 passing)
**Data:** 326 historical maker fills (March-April) + trade tape (May 11-19, 1810 trades) + settlement cache (110 markets, 100% coverage)

## TL;DR

The "flow we were being adversely selected on is captureable as a taker" thesis (Option 3 in [path_b_options.md](path_b_options.md)) **is supported in aggregate** — flipping every historical maker fill to a taker on the opposite side gives **+$10.30 net** after taker fees, vs **-$12.69** for the original maker side. **Delta: +$22.99.**

BUT — three caveats undermine the case for immediate live implementation:

1. **Signal not temporally stable.** Time-split shows early half is -$3.57, late half is +$13.87. The aggregate +$10.30 is driven mostly by the late half.
2. **Trade-tape-validated subset is sharply NEGATIVE (-$33.69).** Using current trade tape direction to choose which fills to flip gives the wrong answer for historical fills.
3. **Hold-to-settle has settlement risk per trade.** We'd be holding individual positions to game completion, accepting per-game directional risk — fundamentally different risk profile from MM.

**Recommendation:** Forward observer for 2-3 weeks before any live trial. The aggregate signal is encouraging enough to keep investigating, but not robust enough to commit capital on.

## Methodology

For each maker fill in `data/poly_mm_live.db`:
- yes_bid maker at price P → taker counterfactual: SELL YES at P (joins the historical YES-seller taker)
  - Settles +P if NO wins; (P-100) if YES wins
- no_bid maker at price P → taker counterfactual: SELL NO at P
  - Settles +P if YES wins; (P-100) if NO wins
- Taker fee: `0.02 × P(1-P) × 100` per contract, applied as cost
- Compare aggregate to maker baseline (hold-to-settle, which the original yes_penalty_counterfactual computed as -$12.69 net after maker rebates)

## Unconditional flip (all 326 historical fills)

```
TOTAL              326    640 $  +13.49 $  +3.19 $  +10.30
                                  settled    fees      net
```

Per-prefix decomposition:

| Prefix | Fills | Settled $ | Fees $ | Net $ | Verdict |
|---|---|---|---|---|---|
| aec-nhl | 91 | -3.72 | +0.91 | **-4.63** | NHL games settled near-50/50 → flipping doesn't capture edge |
| asc-nba | 82 | +0.03 | +0.81 | -0.78 | near-zero edge in this sample |
| tsc-mlb | 34 | +2.94 | +0.33 | **+2.61** | YES-side fills lost → flipping wins |
| asc-cbb | 32 | -0.90 | +0.32 | -1.22 | Slightly negative |
| tsc-nba | 26 | -2.84 | +0.25 | **-3.09** | YES-side fills won → flipping loses |
| aec-atp | 13 | +4.73 | +0.11 | **+4.62** | Strong taker edge on tennis |
| asc-mlb | 10 | +3.96 | +0.10 | **+3.86** | Strong taker edge on MLB alt-spread |
| asc-nhl | 10 | +5.32 | +0.09 | **+5.23** | Strong taker edge |
| aec-mlb | 7 | +4.56 | +0.07 | **+4.49** | Strong taker edge |
| tsc-nhl | 10 | -2.86 | +0.09 | **-2.95** | Wrong side |
| (smaller) | 11 | +1.18 | +0.05 | +1.13 | Mixed |

**Aggregate +$10.30 across 326 fills, $0.032 per fill or ~3.2c per fill, ~$0.016 per contract.**

That's a very thin per-contract edge — survives the taker fee but barely.

## Time-split stability test

Sorted fills by `filled_at` and split into early/late halves:

| Half | Fills | Maker baseline | Taker net | Delta |
|---|---|---|---|---|
| Early (oldest 163) | 163 | (subset of -$12.69) | **-$3.57** | not positive |
| Late (newest 163) | 163 | (subset of -$12.69) | **+$13.87** | strongly positive |

**The aggregate signal is unstable.** If we'd deployed an Option 3 taker strategy at the START of the historical period, the early half says we'd have lost money. The strategy looks good only in retrospect, conditioned on knowing which time period to deploy.

This is the same temporal-instability problem we found in [simulator_recalibration_findings.md](simulator_recalibration_findings.md) — directional flow signals don't transfer across time periods reliably.

## Trade-tape-validated subset (signal alignment test)

Using current trade tape (May 11-19) per-prefix YES_BID share to filter the historical fills:
- For yes_bid maker fills: keep only if current trade tape shows prefix is YES-heavy (yes_bid_share > 0.55) — signals continued YES-seller flow
- For no_bid maker fills: keep only if current trade tape shows prefix is NO-heavy (yes_bid_share < 0.45) — signals continued NO-seller flow

Result: 41/326 fills qualify. Net P&L: **-$33.69** (vs +$10.30 unconditional).

Direction: dramatically WORSE than unconditional.

The validated subset is sharply negative because:
- Current trade tape flagged `asc-nba` as NO-heavy (44.4% yes_bid_share — below 0.45)
- 35 historical asc-nba no_bid fills would be flipped to sell-NO
- Those games happened to settle NO (NO won often in historical asc-nba)
- Selling-NO into NO-winning games = -$29.32 settled loss

This is the same temporal-mismatch problem in reverse: current signal doesn't match historical outcomes.

## What this means for Option 3 implementation

The unconditional aggregate is positive, but every robustness check (time-split, trade-tape-validation) shows the signal isn't reliable. Concrete implications:

### What we can say with confidence

- **The bot WAS on the wrong side of trades on average** — losing $22.99 (maker-vs-taker delta) over 326 fills is a real number.
- **Some prefixes have a consistent taker edge** — aec-atp, asc-nhl, asc-mlb, aec-mlb all showed +$3-5 in the unconditional flip. These deserve targeted attention.
- **Some prefixes (aec-nhl, tsc-nba, tsc-nhl) had the opposite signal** — taker would have lost. These should be excluded or treated differently.

### What we CAN'T say

- **"Option 3 works" as a universal strategy.** The signal isn't stable enough.
- **"The trade tape predicts profitable taker opportunities."** Validated subset goes the WRONG direction.
- **"Holding to settle is safe."** Per-game directional risk would compound (1c expected edge with much higher variance).

## Recommended next experiments

### Experiment 1: Per-prefix taker edge persistence (~1 hour)

Within each prefix that shows positive unconditional taker edge (aec-atp, asc-nhl, asc-mlb, aec-mlb), check:
- Is the edge concentrated in 1-2 markets or spread across all markets in the prefix?
- Is the edge stable over the period (e.g., split each prefix into halves)?

If concentrated in a few markets, the signal is sample-luck. If spread across all markets and stable across time, it's a real structural property of that market type.

### Experiment 2: Forward observer (2-3 weeks, real-time)

Build `scripts/forward_taker_observer.py`:
- Connect to the same WebSocket trade tape collector
- For each market in `data/poly_active_slugs.json`, watch best bid/ask in real time
- When opportunity appears (BBO offset from a computed fair value beyond a threshold), LOG the hypothetical taker fill with timestamp + price + size
- After settlement, compute realized counterfactual P&L
- Compare to the bot's actual realized maker P&L over the same period

This generates the FORWARD-looking data that the historical retrospective can't provide. After 2-3 weeks, we'd have real-time taker counterfactual P&L on current market conditions.

### Experiment 3: Mid-vs-settled bias check (~1 hour)

Per-prefix, compute: for each fill at price P with outcome O, what's the expected per-fill P&L if the OBI "fair" was correct? If maker AND taker both lose on average across many prefixes, the OBI model itself is biased and the bot's edge thesis is fundamentally broken.

This is the test the original Option 3 plan called for: "Is the implied 'fair' actually fair?"

## My updated lean

I previously upgraded Option 3 to HIGH priority based on "the trade tape provides the exact data needed." That upgrade was correct — but the experiment result is **encouraging but not decisive**. The +$22.99 aggregate is real, but the stability concerns are also real.

**Revised recommendation:**

- **Don't commit to live trial yet.** The retrospective shows the signal is real but unstable. Live trial would commit capital based on a signal we know shifts over time.
- **Build the forward observer (Experiment 2).** This is the data we actually need. Costs nothing operationally (it's a passive observer alongside the trade tape collector). After 2-3 weeks, we have a forward-looking signal that doesn't depend on the historical-vs-current temporal mismatch.
- **Run Experiments 1 + 3 quickly** — both are pure analysis on existing data, decisive for narrowing down what to focus the observer on.

If you want to commit to Experiment 2 right now, I can build it. It's a ~2-3 hour task and starts producing data immediately.

If you want a smaller step first, Experiment 1 (per-prefix edge persistence) is ~30 minutes and tells us which prefixes' aggregate positive numbers are sample-luck vs. structural.

## What this DOESN'T close

- **Option 1 (asc-only):** Still demoted. Even within asc, the unconditional taker counterfactual is mixed (asc-nba near zero, asc-cbb negative, asc-nhl/mlb positive). Per-prefix split doesn't cleanly identify a winning subset.
- **Option 2 (Kalshi politics):** Untouched by this work. Still worth pursuing as a parallel data-collection track.
- **Option 4 (single-sided MM):** Not directly tested here. The taker counterfactual assumes paying taker fees; single-sided MM would preserve the maker rebate. Worth a separate analysis.

## Code summary (this commit)

- `scripts/research/taker_counterfactual.py` (new): aggregation + signal-validation + time-split.
- `tests/test_taker_counterfactual.py` (new): 12 tests covering fee math, flip semantics for both sides, settlement direction, aggregate per prefix, unsettled handling.
- 791 total tests passing.

CLI:
```bash
python scripts/research/taker_counterfactual.py                       # unconditional
python scripts/research/taker_counterfactual.py --time-split          # early/late
python scripts/research/taker_counterfactual.py --validate-with-tape  # current signal filter
python scripts/research/taker_counterfactual.py --filter-prefix tsc-mlb
```
