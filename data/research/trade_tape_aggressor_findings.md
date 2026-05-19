# Trade Tape Aggressor Analysis — Findings

**Generated:** 2026-05-19.
**Data:** 1,810 trades captured by `scripts/trade_tape_collector.py` (PID 59079) across 7.17 days (2026-05-11 → 2026-05-19), via Polymarket US WebSocket `SUBSCRIPTION_TYPE_TRADE`.
**Script:** [scripts/research/trade_tape_aggressor_analysis.py](../../scripts/research/trade_tape_aggressor_analysis.py)

## TL;DR

The structural asymmetry hypothesis (YES-seller takers dominate → yes_bid fills outnumber no_bid by ~1.79× → flat 1c YES penalty is justified) is **NOT supported by this week's real trade tape data**. Aggregate across core sport prefixes:

| Metric | Historical (March-April, 326 fills) | Trade tape (May 11-19, 1,810 trades) |
|---|---|---|
| YES_BID drain | 209 (64%) | 522 (36%) |
| YES_ASK drain | 117 (36%) | 923 (64%) |
| Ratio (yes:no equivalent) | **1.79×** YES heavy | **0.57×** (NO heavy) |

**The direction has flipped.** Per-prefix breakdown reveals the asymmetry is highly heterogeneous and was likely a sport-mix artifact in the historical sample.

## Decode of the Polymarket maker.side mapping

From the 1,810 trade tape rows + sample inspection:

- `maker.side=ORDER_SIDE_BUY` → the maker had a **YES_BID** (a buy-YES order). Taker SOLD YES into it. Drains YES_BID liquidity.
- `maker.side=ORDER_SIDE_SELL` → the maker had a **YES_ASK** (a sell-YES order, equivalent to NO_BID conversion). Taker BOUGHT YES from it. Drains YES_ASK liquidity.

So `maker.side=BUY` count is a proxy for "yes_bid hits" market-wide, and `maker.side=SELL` count is a proxy for "no_bid hits".

The `maker.intent` field is UNDEFINED 83% of the time (1509/1810) — not informative for direction. `taker.intent` is consistent with `taker.side` direction and confirms the decoding above.

## Per-prefix aggressor flow

Sorted by trade count, core sport prefixes:

| prefix7 | trades | mBUY (YES_BID hit) | mSELL (YES_ASK hit) | mB:mS | YES_BID share | Verdict |
|---|---|---|---|---|---|---|
| `tsc-nba` | 397 | 77 | 320 | **0.24** | **19%** | NO_BID heavy |
| `tsc-mlb` | 370 | 174 | 196 | 0.89 | 47% | BALANCED |
| `aec-wta` | 226 | 69 | 157 | 0.44 | 31% | NO_BID heavy |
| `astatc-mlb` | 155 | 138 | 17 | **8.12** | **89%** | **YES_BID heavy** |
| `aec-nhl` | 151 | 74 | 77 | 0.96 | 49% | BALANCED |
| `aec-mlb` | 143 | 63 | 80 | 0.79 | 44% | NO_BID heavy |
| `asc-nba` | 72 | 32 | 40 | 0.80 | 44% | NO_BID heavy |
| `aec-cs2` | 70 | 27 | 43 | 0.63 | 39% | NO_BID heavy |
| `aec-wnba` | 44 | 20 | 24 | 0.83 | 45% | BALANCED |
| `aec-atp` | 26 | 21 | 5 | **4.20** | **81%** | **YES_BID heavy** |
| `tsc-nhl` | 26 | 10 | 16 | 0.62 | 38% | NO_BID heavy |
| `asc-mlb` | 14 | 0 | 14 | 0.00 | 0% | NO_BID heavy (n=14, all SELL) |
| `atc-lal` | 14 | 1 | 13 | 0.08 | 7% | NO_BID heavy |

**Patterns:**
- The three biggest sport prefixes are NO_BID heavy or balanced (`tsc-nba`, `tsc-mlb`, `aec-wta`, `aec-nhl`, `aec-mlb`)
- Two niche prefixes are strongly YES_BID heavy (`astatc-mlb` = MLB Yes-Run-First-Inning, `aec-atp` = ATP tennis)
- `tsc-nba` flipped most dramatically — historically YES heavy (20:6 = 3.33×), now strongly NO heavy (19% YES_BID share)

## Cross-period comparison (same prefix7)

Where both historical fills and trade tape have data:

| prefix7 | Historical (Mar-Apr) | Trade tape (May 11-19) | Direction |
|---|---|---|---|
| `tsc-nba` | 20:6 (yes heavy 3.33×) | 77:320 (no heavy 0.24×) | **FLIPPED** |
| `tsc-mlb` | 28:6 (yes heavy 4.67×) | 174:196 (balanced 0.89×) | **FLIPPED** (to bal) |
| `tsc-nhl` | 7:3 (yes heavy 2.33×) | 10:16 (no heavy 0.62×) | FLIPPED |
| `aec-nhl` | 46:45 (balanced 1.02×) | 74:77 (balanced 0.96×) | CONSISTENT |
| `asc-nba` | 47:35 (yes heavy 1.34×) | 32:40 (no heavy 0.80×) | FLIPPED (mild) |
| `aec-mlb` | 7:0 (all yes — n too small) | 63:80 (no heavy 0.79×) | n/a |
| `aec-atp` | 12:1 (yes heavy 12×) | 21:5 (yes heavy 4.20×) | CONSISTENT direction |

**5 of 7 comparable prefixes show direction flips** between the two periods. The two that stayed consistent are `aec-nhl` (balanced both periods) and `aec-atp` (yes heavy both periods).

## Implications for Path C strategy

The Path C YES penalty design assumed a UNIVERSAL YES-seller dominance applicable to all markets. The trade tape data shows:

1. **The 1.79× historical aggregate was an aggregation artifact**, not a market property. Per-prefix data shows highly heterogeneous flow direction even in the historical sample (`tsc` 3.67× YES heavy vs `aec` 1.57× vs `asc` 1.39× — see [yes_penalty_counterfactual.md](yes_penalty_counterfactual.md) Appendix A).

2. **The direction is time-varying.** Most prefixes have flipped between March and May. This week's MLB-heavy market mix has different aggressor flow than the March NCAA basketball mix.

3. **For most current prefixes, a flat 1c YES penalty is the WRONG SIGN.** `tsc-nba` (19% YES_BID share) actually has takers BUYING YES heavily — what the bot would want is to be MORE aggressive on YES_BID, not less. A NO_BID penalty would be more appropriate.

4. **Two prefixes (`astatc-mlb`, `aec-atp`) DO show YES-heavy flow** consistent with the original thesis. The penalty would apply correctly there.

5. **The previously recommended `{"tsc": 1}` differential** is now suspect. `tsc-nba` and `tsc-mlb` have different aggressor profiles this week (19% and 47% YES_BID share), and both differ from the historical 3.67× signal.

## What this changes

### What's invalidated

- The "tsc-only flat penalty" recommendation from [roundtrip_simulator_findings.md](roundtrip_simulator_findings.md) Appendix B. It was built on historical fill ratios which may have been sample-period-specific.
- The static survival model in [roundtrip_simulator.py](../../scripts/research/roundtrip_simulator.py) (with constants 0.20/0.40/0.60 etc.) which doesn't capture the time-varying flow direction.

### What's still valid

- The simulator's mechanic (FIFO pair-off, last-snapshot exit pricing, maker rebate correction) is correct regardless of survival model
- The trade tape collector pipeline works
- The differential-penalty support in the simulator (`make_differential_survival_fn`) is the right API — just needs new empirical inputs
- The kill-criterion framework (use live or counterfactual on live, never paper) is correct

### What's needed before Phase B step 2

Before implementing any penalty change in `src/mm/state.py`, we should:

1. **Build a dynamic/adaptive penalty mechanism** keyed on recent trade tape flow direction, not on stale prefix categories. E.g., for each market: compute the last-7-days `maker.side BUY:SELL` ratio, and set the penalty direction (YES, NO, or none) accordingly.

2. **Re-run the round-trip simulator with the new trade-tape-informed survival rates** rather than the historical 0.20/0.40/0.60 constants. The new rates should be:
   - For prefixes where trade tape says yes_bid_share < 50% (NO_BID drain heavy): higher YES_BID survival (penalty unjustified)
   - For prefixes where yes_bid_share > 50% (YES_BID drain heavy): lower YES_BID survival (penalty justified)
   - This calibration uses real flow data instead of assuming a uniform 1.79× signal

3. **Decide whether to scope the bot to specific prefixes** based on flow consistency. Some prefixes (`aec-nhl`, `aec-atp`) had consistent flow across periods; others (`tsc-nba`) flipped completely. Scoping to consistent prefixes reduces strategy uncertainty.

## Operational issues uncovered

### Bot is trading weather markets (out of scope)

The paper bot has 126 fills across 7 `tc-temp-*` markets (temperature forecast markets like "LAX high temp 67-68°F"). These are not sports — CLAUDE.md scopes the bot to "NBA, NHL, MLB, NCAA spreads/totals". The scanner is selecting these because they pass the spread/volume filters, but they have completely different dynamics (information arrival, settlement mechanics) than sports.

**Recommendation:** Add a sport-whitelist filter in `scripts/poly_daily_scan.py` similar to the existing `scan_today_sports()` filter, or reject markets matching `tc-temp-*` slug pattern.

### Paper bot fill ratio diverges from trade tape

Paper bot last 7 days: `tsc-mlb` y:n = 1.25 (YES heavy)
Trade tape `tsc-mlb`: maker.side BUY:SELL = 0.89 (balanced)

The 0.36 gap is consistent with the [paper_vs_live_gap.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/paper_vs_live_gap.md) finding: paper's `drain_queue()` over-fills the yes_bid side. This further confirms paper P&L/ratio is not strategy evidence.

## Next steps (revised Phase B plan)

1. **Don't implement the previously-recommended `{"tsc": 1}` differential penalty yet.** The trade tape data doesn't support the assumption that tsc is consistently YES-heavy.

2. **Build aggressor-aware drain_queue calibration** as a research first step. Compute per-prefix survival rates from the trade tape; plug them into the simulator; rerun to see if any prefix-specific penalty configuration shows positive predicted P&L.

3. **Defer live trial** (Phase B Option B). The simulator's prediction needs to be re-calibrated against trade-tape-informed inputs before committing live capital.

4. **Add a sport-whitelist filter** to the scanner (separate small task) to prevent weather markets from polluting the analysis.

5. **Continue the trade tape collector running.** Another 1-2 weeks of data will help distinguish "current-week aberration" from "structural recent change."

6. **Re-examine the strategy thesis.** If aggressor flow is highly heterogeneous and time-varying, a fixed-penalty MM may not be the right strategy at all. Worth considering alternatives from [path_b_options.md](path_b_options.md) (Kalshi politics, same-platform taker role).
