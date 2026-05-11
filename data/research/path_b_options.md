# Path B Research — Alternatives Under Investigation

**Status:** Preliminary. Run in parallel to the 4-week paper-trading
window for Path C ([KILL_CRITERION.md](KILL_CRITERION.md)). No
implementation yet — just analysis with concrete next steps so that,
if Path C is killed, we have a primed alternative.

**Generated:** 2026-05-10.
**Inputs:** `data/poly_mm_live.db` (326 historical fills), `data/mm_paper.db`
(Kalshi paper, 40 fills, sports-only), [fill_asymmetry_diagnosis.md](fill_asymmetry_diagnosis.md).

## TL;DR

Three alternatives ranked by tractability:

1. **Market-type split within Polymarket sports** — partial real data already
   exists. `asc` (alt-spread+) shows a 1.39× yes/no ratio (near balance);
   `tsc` (totals) is the worst at 3.67×. **Quick win candidate.**
2. **Kalshi politics** — flow-symmetry hypothesis is plausible but
   untested on our data. Requires a fresh data-collection pass.
3. **Same-platform taker** — captures the YES-seller flow we're being
   adversely-selected on. Profitable in theory but requires paying taker
   fees instead of earning rebates; needs explicit math before any code.

## Option 1 — Market-type split

### What we have

Polymarket slug prefixes break down the 326 production fills:

| Prefix | Market type            | Total | yes_bid | no_bid | Ratio | Notes |
|--------|-----------------------|-------|---------|--------|-------|-------|
| `aec`  | Moneyline             | 118   | 72      | 46     | 1.57× | Borderline asymmetric |
| `asc`  | Alt-spread+           | 134   | 78      | 56     | 1.39× | **Near balanced** (Path C target window) |
| `atc`  | Alt-spread (binary)   | 4     | 4       | 0      | ∞     | Too few fills |
| `tsc`  | Totals (over/under)   | 70    | 55      | 15     | 3.67× | Worst — likely toxic |

Interpretation: the headline 209/117 = 1.79× imbalance is **not uniform**
across market types. `asc` is already inside the "rebalanced" window of
the kill criterion *without* the Path C fixes; `tsc` is 2.5× worse than
the headline. If the bot ran on `asc` only, it might already be viable.

### Hypothesis

Different sports market structures attract different taker cohorts:
- `tsc` (totals): retail "over" bettors flood in, dumping NO when
  game-flow shifts away from their lean — disproportionate YES-bid hits.
- `aec` (moneyline): two-sided position-takers but with house-money
  YES-lean (favorite is YES).
- `asc` (alt-spread+): finer-grained lines that don't pull the same
  retail crowd — closer to professional flow.

### Next steps if Path C is killed

1. Re-run the scanner with a `--market-types asc` filter (or extend
   `apply_prefilters` to accept a whitelist). One-line change.
2. Re-evaluate per-side telemetry on `asc` only after a 2-week paper run.
3. If `asc` shows 1.0-1.4× ratio AND positive net P&L: ship it as the
   restricted strategy. Drop `tsc` and `aec` entirely.
4. Open question: small sample for `atc`. If filling out, may be the most
   symmetric subset (binary alt-spread = explicit two-sided positioning).
   Needs more data before evaluation.

### Effort estimate

Lowest of the three — one filter change, plus existing telemetry/kill
criterion infra works as-is.

## Option 2 — Kalshi politics

### What we have

[Kalshi paper DB](../mm_paper.db) holds 40 fills from sports markets
only (tickers all KXNHL/KXNBA/KXMLB/KXNCAA). The yes/no fill ratio
there is 21/18 = 1.17× — much more balanced than Polymarket sports
(209/117 = 1.79×). But:
- N=40 is too small for inference.
- All Kalshi paper fills are *sports*, not politics — so this number
  doesn't test the politics-flow-symmetry hypothesis.
- Paper fills come from queue simulation, not real taker flow.

### Hypothesis

Politics markets on Kalshi (and Polymarket) have different participant
mix from sports:
- Longer time horizons → buyers and sellers exist for both YES and NO.
  Sports has a single "outcome day"; politics has months.
- Sentiment-driven both directions → both partisans place positions on
  both sides (not just position-dumpers exiting).
- Lower retail flow → less of the "I had a hunch" panic-sell pattern
  that produces YES-side adverse selection in sports.

### Next steps

1. Run `scripts/kalshi_daily_scan.py` for politics-category markets
   for a week to enumerate candidates and observe spreads/depths.
2. Pull trade history (Kalshi API supports historical trades per market)
   for 10-20 high-volume politics markets. Bucket trades into
   "YES-buyer initiated" vs "NO-buyer initiated" and compute the ratio.
3. If ratio is closer to 1.0× than sports' 1.79×, that's the green
   light to paper-trade Kalshi politics with the same engine
   (no skew changes — Kalshi is the engine's original target platform).
4. Bankroll: Kalshi requires its own funding. Decide before committing.

### Effort estimate

Medium. Existing Kalshi infrastructure (`src/kalshi_client.py`,
`scripts/paper_mm.py`, `scripts/kalshi_daily_scan.py`) is intact and
working. Main work is data analysis + a paper-trade evaluation cycle.

### Risk

Kalshi has different fee structure (`maker_fee = +0.0175 × P(1-P) × 100`
— positive cost, not a rebate). Even balanced flow may not be
profitable if the underlying spreads are thin. Need to verify post-fee
economics with real spread data, not just ratio data.

## Option 3 — Same-platform taker

### What we have

The diagnosis: YES-sellers on Polymarket sports hit our YES bid at 49c
when fair is ~50c. We're being adversely-selected.

**Logical converse:** if YES-sellers consistently sell below fair,
then a buyer who lifts their offer at 49c captures (fair - 49c)
*as a taker*. The taker pays fees (`0.07 × P(1-P) × 100`, ~0.7c per
contract at P=50), so:

  taker edge = (fair - fill_price) − taker_fee
             ≈ (50 − 49) − 0.7 = +0.3c (positive but thin)

### Hypothesis

The flow we're being adversely-selected on IS the flow we'd capture as
a taker. Same market, opposite role. The behavioral tax we lose as a
maker, we'd earn as a taker.

### Open questions (must answer before any code)

1. **Is the implied "fair" actually fair?** Per Phase 1.2, our bot's
   fills hold-to-settle at −2.11c per contract. That means at our entry
   prices, the *settlement value* averages less than entry. If "fair"
   from OBI is misleading us, taker edge is also misleading.
2. **Frequency vs ours.** As a maker we wait passively in queue. As a
   taker we'd be lifting offers in real-time. Are there enough offers
   to lift to make the strategy fill > 1×/hour per market?
3. **Capital efficiency.** Maker rebates make the breakeven low;
   taker fees raise it. The 0.3c estimate above is fragile to:
   spread compression (often <2c near game start), fair-value drift
   between OBI tick and order placement, slippage on size>1.

### Next steps

1. Theoretical: compute the historical taker P&L on our exact 326 fills.
   If we had been the taker hitting THEIR offers at our fill prices,
   what would the realized hold-to-settle have been? (Inverse of
   Phase 1.2 calc.) If aggregate positive after taker fees, the thesis
   holds; if negative, the "fair" was indeed wrong.
2. If positive: build a tiny prototype that observes (not places) taker
   opportunities for a week, logging counterfactual P&L.
3. If counterfactual is profitable: paper trade.

### Effort estimate

Highest. This is essentially a different strategy than market-making
(directional with sub-second timing), not just a parameter tweak.

## Recommended order

If Path C kill fires:

1. **Quick experiment first**: option 1 (`asc`-only restriction). One-week
   paper trade to confirm the `asc` 1.39× ratio holds and economics turn
   positive after Path C fixes also help.
2. **In parallel**: data collection for option 2 (Kalshi politics) — no
   trading commitment yet.
3. **Defer option 3** unless 1 and 2 both fail. The counterfactual analysis
   in step 1 above should be done first as it's the cheapest test.

## What this doc isn't

- **A pre-commitment.** Path C might succeed. The kill criterion is the
  gate; this doc just makes sure we have a tested alternative ready
  if it fails.
- **A capital allocation plan.** Doesn't address bankroll-sizing or
  multi-platform exposure. Punted to whoever picks an option.
- **Final.** Re-evaluate at end of Path C window with fresh per-side
  telemetry from the 4-week paper run.
