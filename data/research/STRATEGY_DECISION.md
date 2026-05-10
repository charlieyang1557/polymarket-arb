# Phase 1.4 — Strategy Decision Synthesis (REVISED)

**Generated:** 2026-05-10
**Status:** REVISED after user pointed to actual production P&L data
**Inputs:** [calibration_summary.md](calibration_summary.md), [settlement_pnl_report.md](settlement_pnl_report.md), [round_trip_viability.md](round_trip_viability.md), live DB

## TL;DR (revised)

**Recommendation: STRATEGY IS EMPIRICALLY FAILING. Do NOT pivot to hold-to-settle. Either fix the underlying issues (fee accounting, pair tracking, inventory management) and re-test, or kill the strategy honestly.**

The original recommendation ("keep current strategy") was wrong. After the user noted they had terminated the bot a week ago for poor performance, the live database confirms the bot lost approximately **$2.92 net** across 34 sessions (March 31 – April 24, 2026), about 12% drawdown on the $25 bankroll. The Phase 1.3 "viable" verdict was misleading because it measured entry-side spread capture (+0.27¢ on average) but did not account for the much larger exit costs from inventory accumulation that couldn't be offset before game start.

## What the production data actually shows

From `mm_snapshots` (last snapshot per session-market):

| Metric | Value |
|---|---|
| Sessions | 34 |
| Total fills | 326 |
| Sum of realized P&L | −13.84¢ |
| Sum of unrealized at exit | **−123.00¢** |
| Sum total P&L (realized + unrealized) | −136.84¢ |
| Total fees paid | 154.84¢ |
| **Net P&L (total − fees)** | **≈ −$2.92** |

### Inventory at exit — where the losses came from

| Inventory state | n market-sessions | Sum unrealized at exit |
|---|---|---|
| Flat (inv=0) | 237 | $0.00 |
| Small unhedged (\|inv\|≤2) | 81 | **−$0.87** |
| Medium unhedged (\|inv\|≤5) | 9 | **−$0.36** |

**90 of 327 market-sessions ended with non-zero inventory.** Every one of those losing categories accumulated *negative* unrealized P&L on average — the inventory we couldn't pair off settled against us. This is exactly the failure mode the user described: "cannot fill the round trip and losing money."

### Worst sessions (by final total_pnl)

| Date | Final total | Likely failure mode |
|---|---|---|
| Apr 10 | −47.5¢ | 3 markets with realized losses (cross-tick stop-losses), several markets stuck at inv=2 |
| Apr 18 | −40.5¢ | 10 markets ended with inv=2; mark-to-market negative on most |
| Apr 13 | −25.2¢ | inventory stuck at exit |
| Apr 14 | −21.0¢ | inventory stuck at exit |
| Apr 19 | −13.0¢ | inv=2 at session end with negative unrealized |

A repeating pattern: bot accumulates 2-contract inventory it can't offset, exits force-close before game start, takes the loss. CLAUDE.md calls this "structural stop-losses, not bugs" — but the *frequency* of this failure mode is what makes the strategy unprofitable, not any single occurrence.

## Phase 1 findings — corrected interpretation

### Phase 1.1 — Calibration on resolved sports
- Sample: 39 markets in 30-70¢ band (plan threshold was 500). Verdict: **aggregate-only**, treat as preliminary.
- Aggregate gap: −27pp (suggests YES overpricing).
- **Status: weak signal, undersampled, doesn't matter for the main decision.**

### Phase 1.2 — Counterfactual settlement P&L on bot fills
- All-fills counterfactual: **−2.11¢/contract**, win rate **46.6%** (paper benchmark: +1.91¢, 63.4%).
- Defensive subset (unhedged at game-start): **−8.63¢/contract**, win rate 39.6%.
- yes_bid loses (−4.62¢, WR 43.9%); no_bid wins (+2.31¢, WR 51.3%).
- 209 yes_bid vs 117 no_bid → bot is adversely selected onto the losing side.
- **Status: hold-to-settle thesis fails on our fill distribution.**

### Phase 1.3 — Round-trip viability test (CORRECTED INTERPRETATION)
- Phase 1.3 reported "viable" based on entry-side half-spread capture (+0.27¢ overall, +0.91¢ in 45-55¢ band) > observed informed share.
- **This was incomplete.** Phase 1.3 measured the spread captured *at fill*, not the spread realized *after exit*. In production, the bot couldn't reliably offset fills — accumulated inventory force-exited at adverse prices or held to settlement at a loss.
- The actual realized round-trip P&L was strongly negative (−$2.92 over 34 sessions), so the empirically correct verdict for Phase 1.3 is **NEGATIVE, not viable**.

## Decision matrix — corrected

| Calibration | Round-trip | Counterfactual | Recommendation |
|---|---|---|---|
| Loose (>3pp) | Viable | Positive | Offensive hybrid |
| Loose (>3pp) | Marginal/negative | Positive | Pivot fully |
| Tight (<3pp) | Viable | ~Zero | Keep current |
| **Tight* / Loose-but-undersampled** | **Negative (in production)** | **Negative** | **Kill or rebuild** |
| Loose (>3pp) | Negative | Positive but <1¢ | Defensive-only |

\* The Phase 1.1 loose signal can't be operationalized on our fill distribution (Phase 1.2 confirms), so for decision purposes treat as effectively tight.

**Our position: row 4 (revised). KILL or REBUILD.**

## Why hold-to-settle would not have rescued us

The user's intuition was: "if round-trip is failing, maybe holding to settlement captures the behavioral tax instead." Phase 1.2 directly tests this counterfactual on the same fills:

- All-fills hold-to-settle: **−$13.49** (worse than the actual −$2.92)
- Defensive-only hold-to-settle: **−$18.29** (much worse)
- yes_bid fills are adversely selected onto the losing settlement side. Holding to settlement on these fills compounds the loss.

The behavioral tax exists in the calibration sample (Phase 1.1's −27pp aggregate). It does NOT exist in the bot's actual fill distribution because:
1. The bot's scanner picks the most efficient (least mispriced) markets — selection works against finding edge.
2. The bot's quote symmetry has been broken in practice — getting filled almost 2:1 on YES bids vs NO bids, putting us on the *wrong* side of the calibration bias.
3. Sports markets are structurally closer to broad-based than single-name (paper Appendix B), so the available behavioral edge is smaller than the paper's headline.

## Two side-issues that may explain part of the loss

These are independent diagnostics that surfaced during Phase 1; fixing them won't necessarily save the strategy but they're real bugs:

1. **Fee accounting bug**: [src/mm/state.py:97](src/mm/state.py:97) `maker_fee_cents()` uses Kalshi's positive-cost formula (`0.0175 × P × (1−P) × 100`). Polymarket sports makers earn a *rebate* (negative). [src/poly_client.py:145](src/poly_client.py:145) has the correct formula but the engine doesn't use it. Estimated impact: bot's reported P&L underreports actual cash by ~$3.66 over 326 fills (~14% of bankroll). **The bot may have been less unprofitable than reports suggest, but probably not enough to flip the sign.** Real economics need to be recomputed with correct fees.

2. **`pair_pnl` and `pair_id` are NULL/0 for all 326 fills**. The pair-off tracking logic in `pair_off_inventory` either isn't being invoked or isn't writing back. This means the bot has NO realized round-trip P&L attribution per fill — only aggregate per-session P&L from `mm_snapshots`. Without per-pair attribution, debugging the strategy is much harder.

## Recommendations

### Don't:
- Implement the offensive hold-to-settle hybrid (Phase 3-6 of plan). Phase 1.2 says it would lose more.
- Pivot to defensive hold-to-settle. The defensive subset counterfactual is even worse (−8.63¢/contract).
- Ignore the production loss data and continue trading on the hope of recovery. The bot was running for ~25 sessions of the 34, and the trend is clearly negative.

### Do (in order of immediacy):

1. **Fix the fee bug** (state.py:97). Recompute all session P&L with correct Polymarket rebate. See if any sessions cross from negative to positive after correction. ~$3.66 over 326 fills means up to ~$0.011 per fill, or ~$3-4 of underreported P&L.

2. **Fix the pair_pnl tracking**. Without per-pair attribution we cannot diagnose strategy failures.

3. **Investigate the asymmetric fill rate**. Why did we get 209 yes_bid vs 117 no_bid? Is the OBI microprice or skew logic systematically pulling our YES bid into the spread more aggressively? Is the issue that takers more often want to *sell* YES than *buy* it on these markets? If we can't fix the asymmetry, the strategy is dead-on-arrival regardless of paper edge.

4. **Tighten market selection**. Currently the scanner picks markets with `net_spread ≥ 1`. With 0.27¢ realized half-spread capture, that's far too generous — we need genuine 3+¢ effective spread to leave room for adverse selection and exit costs. Recompute the scanner threshold from observed costs.

5. **Only after 1-3 are done**: re-run a 2-week paper-trade evaluation. If the bot can produce consistent positive net P&L (even small) under the corrected fee model and tighter market selection, then continue. If it cannot, KILL THE STRATEGY HONESTLY per CLAUDE.md's empirical-first principle.

### Reconsider hold-to-settle if (and only if):
- We accumulate ≥500 in-band resolved markets in `data/historical/price_history.json` (or equivalent fresh source) and re-run Phase 1.1 calibration with proper sample size
- The bot's fill distribution rebalances toward symmetry (yes_bid ≈ no_bid count) — this would change the Phase 1.2 counterfactual sign
- We accept a fundamentally different strategy: not market-making but pre-flagged contrarian betting on miscalibrated markets, with explicit acceptance of high variance

## What this means

The empirical answer is "stop hoping the paper applies and look at what's broken." The user's manual termination decision was correct. This research has produced:

- A diagnosis of *why* the bot was losing (inventory imbalance + adverse selection + small spread captured)
- Empirical refutation of the hold-to-settle thesis on this fill distribution
- Two real bugs flagged for separate fix
- A clear empirical justification for not pivoting to a more capital-intensive strategy

The honest path forward is fix-and-retest, with a hard kill threshold if fixes don't produce consistent positive P&L within 2 weeks of paper trading.

## Files

- [calibration_summary.md](calibration_summary.md) — Phase 1.1 detail
- [miscalibration_flags.json](miscalibration_flags.json) — per-bucket data
- [settlement_pnl_report.md](settlement_pnl_report.md) — Phase 1.2 detail
- [settlement_pnl_summary.json](settlement_pnl_summary.json) — full P&L data
- [resolved_markets_cache.json](resolved_markets_cache.json) — SDK lookup cache (110 slugs)
- [round_trip_viability.md](round_trip_viability.md) — Phase 1.3 detail (see CORRECTED INTERPRETATION above)
- [round_trip_viability.json](round_trip_viability.json) — full viability data

## Revision history

- **v1 (original)**: Recommended "keep current strategy" based on Phase 1.3 alone showing theoretical viability.
- **v2 (this revision)**: User noted the bot was terminated for losing money. Live DB confirms ~$2.92 net loss over 34 sessions. Phase 1.3 verdict was incomplete (entry-only economics); production data shows strategy is empirically failing. Recommendation revised to fix-and-retest or kill.
