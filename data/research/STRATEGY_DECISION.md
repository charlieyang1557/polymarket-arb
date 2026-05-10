# Phase 1.4 — Strategy Decision Synthesis

**Generated:** 2026-05-10
**Inputs:** [calibration_summary.md](calibration_summary.md), [settlement_pnl_report.md](settlement_pnl_report.md), [round_trip_viability.md](round_trip_viability.md)

## TL;DR

**Recommendation: KEEP CURRENT STRATEGY (round-trip MM with pre-game exit). Do NOT pivot to offensive hold-to-settle.**

The empirical evidence does not support the offensive hybrid hypothesis on our data. Phase 1.2's direct counterfactual on our 326 actual fills shows hold-to-settle would have **lost** $13.49 (mean −2.11c per contract, win rate 46.6%) — far below the paper's +1.91c / 63.4% single-name benchmark. This single finding overrides the suggestive Phase 1.1 calibration signal.

## Findings

### Phase 1.1 — Calibration on resolved sports (vs realized settlement)
- Sample: only **39 markets** in 30-70c band (plan threshold was 500). Verdict: aggregate-only.
- Aggregate gap: **−27.03pp** (realized YES rate 18% vs implied 45%) — strongly suggestive of YES overpricing.
- Per-bucket: 3 of 6 buckets statistically significant despite small samples.
- **Caveat**: tiny sample, different population than our trading universe (those 39 markets aren't ones we quoted).

### Phase 1.2 — Counterfactual settlement P&L on bot fills
- Sample: 326 fills across 110 markets, 100% resolved.
- All-fills counterfactual: **−2.11c/contract**, win rate **46.6%** (paper benchmark: +1.91c, 63.4%).
- Defensive subset (unhedged at game-start): **−8.63c/contract**, win rate 39.6%.
- Side breakdown:
  - `yes_bid` (long YES): −4.62c, WR 43.9% — **consistent with calibration's YES overpricing**
  - `no_bid` (long NO): +2.31c, WR 51.3% — modest edge
- Asymmetric fill distribution: 209 yes_bid vs 117 no_bid — bot is adversely selected onto the losing side.
- Post-haircut: still −2.11c (haircut only reduces upside, leaves losses).

### Phase 1.3 — Round-trip viability (paper Section 4.2.3)
- Sample: 326 fills, but only 24 with full post-fill snapshot coverage.
- Overall: half_spread captured = +0.27c, α_tolerance = 0.084, α_observed = 0.021 → **viable**.
- 45-55c band (where we trade most, n=85 with coverage): half_spread = +0.91c, α_tolerance = 0.317, α_observed = 0.059 → **viable with more confidence**.
- **Caveat**: 302/326 fills had insufficient post-fill data (markets deactivated, gaps in tick loop). α_observed likely understated.

## Decision matrix (from plan)

| Calibration | Round-trip | Counterfactual | Recommendation |
|---|---|---|---|
| Loose (>3pp) | Viable | Positive | Offensive hybrid |
| Loose (>3pp) | Marginal/negative | Positive | Pivot fully |
| Tight (<3pp) | Viable | ~Zero | Keep current |
| Tight (<3pp) | Negative | Negative | Kill |
| Loose (>3pp) | Negative | Positive but <1¢ | Defensive-only |

**Our combination: Loose calibration | Viable round-trip | NEGATIVE counterfactual.**

This is not in the matrix as written — it's a sixth combination. The honest interpretation: calibration is loose in the historical sample, but our fill distribution extracts the **wrong side** of that miscalibration. **The decision-relevant test (Phase 1.2) failed.** This is functionally equivalent to row 3 ("Keep current") because the empirical foundation for the pivot doesn't hold for *our* universe.

## Why the calibration signal didn't translate

Three plausible reasons hold-to-settle counterfactual is negative even though calibration shows YES overpricing:

1. **Sample mismatch.** The 39 in-band markets in `price_history.json` are not the ones our bot trades. Our scanner specifically picks markets with tight spreads, good depth, and high trade frequency — exactly the markets where retail miscalibration is *least* likely to persist (efficient prices = no edge).

2. **Adverse fill selection.** Our bot got 209 `yes_bid` fills and only 117 `no_bid` fills (1.79× imbalance). If YES is overpriced, takers are willing to *buy* YES at high prices, which doesn't fill our YES bids — instead, takers selling YES (often informed sellers) hit our `yes_bid`. We end up long YES on the side where YES is overpriced and tends to lose. The calibration is real but the bot is positioned against it.

3. **Selection effect of liquid markets.** Paper Appendix B Table B.2 shows sports has roughly half the informed price impact of single-name. Our bot trades the most liquid sports markets, plausibly the *most* efficiently priced subset — closer to broad-based than to single-name. The calibration finding from a wider sample doesn't carry over.

## Side issues uncovered (separate from main decision)

Two bugs surfaced during Phase 1 that warrant attention regardless of strategy direction:

1. **Fee accounting bug**: `src/mm/state.py:97` `maker_fee_cents()` uses Kalshi's positive-cost formula (`0.0175 × P × (1−P) × 100`). Polymarket sports makers earn a *rebate* (negative). Per `src/poly_client.py:145` `calculate_maker_fee()` the correct formula returns negative. Engine at [src/mm/engine.py:620](src/mm/engine.py:620) uses the Kalshi version. Estimated underreport: ~$3.66 over 326 fills (~14% of $25 bankroll). **Worth fixing.**

2. **`pair_pnl` is NULL for all 326 fills** in the live DB. Either pair-off logic isn't running, or pair_pnl isn't being populated when pairs complete. We had no realized round-trip P&L to compare against the counterfactual. **Worth investigating.**

## Recommendations

**Do:**
- Keep the current pre-game-only round-trip strategy
- Fix the fee accounting bug (use Polymarket rebate formula)
- Investigate why `pair_pnl` is NULL — restore round-trip P&L tracking
- Increase mm_snapshot retention (don't deactivate markets so quickly post-fill) so future Phase 1.3 analysis has better coverage

**Do not:**
- Implement the offensive hold-to-settle hybrid (Phase 3-6 of plan)
- Implement asymmetric quoting on flagged markets
- Add capital reservation logic for hold-to-settle pool

**Reconsider hold-to-settle if:**
- We accumulate ≥500 in-band resolved markets in `price_history.json` (or equivalent fresh source) and re-run Phase 1.1 calibration
- The fee bug fix changes round-trip economics meaningfully
- We observe shifts in market structure (e.g., new market types added, retail flow share changes)

## What this means in concrete terms

Per CLAUDE.md's empirical-first principle: the data says "no" to hold-to-settle on our fills. The honest answer is to accept that and continue refining the round-trip strategy on its own merits. The paper's framework was a useful prompt to investigate, but it doesn't apply to our specific universe (sports + liquid + filtered).

The user's stated direction was "if our goal is to maximize profit, we should hold to settle sometimes." The empirical answer is: **not on these fills, not at this scale, not with this market selection**. The bot's actual fill distribution would have lost money under hold-to-settle.

The work in Phase 1 wasn't wasted — it produced:
- A calibration analysis we didn't have before (suggestive even if undersampled)
- Confirmation that round-trip is viable at our scale (Phase 1.3)
- Two bugs flagged for follow-up (fee accounting, pair_pnl)
- A clear empirical justification for sticking with the current strategy (rather than vague intuition)

## Files

- [calibration_summary.md](calibration_summary.md) — Phase 1.1 detail
- [miscalibration_flags.json](miscalibration_flags.json) — per-bucket data
- [settlement_pnl_report.md](settlement_pnl_report.md) — Phase 1.2 detail
- [settlement_pnl_summary.json](settlement_pnl_summary.json) — full P&L data
- [resolved_markets_cache.json](resolved_markets_cache.json) — SDK lookup cache (110 slugs)
- [round_trip_viability.md](round_trip_viability.md) — Phase 1.3 detail
- [round_trip_viability.json](round_trip_viability.json) — full viability data
