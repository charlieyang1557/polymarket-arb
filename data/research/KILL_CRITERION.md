# Kill Criterion — Path C → Path B Trigger

**Status:** Active as of 2026-05-10. **Revised 2026-05-11** after
empirical finding that paper MM systematically over-fills (~10× rate)
and under-asymmetric (~1.0 yes/no vs live's 4.5×) — paper alone is
not predictive of live. See [memory/paper_vs_live_gap.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/paper_vs_live_gap.md).
**Owner:** Auto-checked at end of every paper-trading session for
operational health; strategy decisions require live counterfactual.

## Context

Path C is "apply cheap fixes (engine fee DI, pair tracking, YES penalty,
banker's rounding, scanner 45-55c, per-side telemetry) + 4-week paper
validation, with parallel research into Path B alternatives." This
document defines the explicit, falsifiable criterion for declaring
Path C dead and moving to Path B (alternative strategies — Kalshi
politics, same-platform taker, market-type split).

Source thesis: [STRATEGY_DECISION.md](STRATEGY_DECISION.md) revised
v2 found ~$2.92 net loss over 34 sessions on a $25 bankroll (~12%
drawdown). [fill_asymmetry_diagnosis.md](fill_asymmetry_diagnosis.md)
H2 identified structural YES-seller taker flow as the primary cause.
The Path C fixes target this asymmetry directly; if they fail to
restore the fill ratio or restore it but underlying economics remain
unprofitable, the strategy is empirically dead-on-arrival.

## Measurement window

**Paper trading is for operational shakedown only** (does the bot
run, does telemetry populate, does pair_pnl persist, does Discord
emit). It is NOT a strategy validation gate — see
[memory/feedback_live_is_ground_truth.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/feedback_live_is_ground_truth.md)
(user feedback 2026-05-11: paper is "not even a reliable reference"
for strategy purposes because `drain_queue()` over-fills 10x and
symmetrizes the yes:no ratio). Sessions use
`scripts/poly_paper_mm.py` and any P&L or ratio numbers from those
runs MUST be discarded for strategy decisions.

**Strategy validation sources, in order of trust:**

- **Realized live trading data** (`poly_mm_live.db`): ground truth.
- **Counterfactual on live data**: e.g.,
  [scripts/research/roundtrip_simulator.py](../../scripts/research/roundtrip_simulator.py)
  — replays the 326 real fills with configurable survival/quote
  hypotheses. This is the right tool for "should we change parameter
  X" questions. See
  [data/research/roundtrip_simulator_findings.md](roundtrip_simulator_findings.md)
  for the round-trip findings under each survival model.
- **Aggressor-aware live data**: Step 4 (WebSocket taker-side
  collector) will provide this once built — proper queue dynamics
  and VPIN toxicity gating become possible.
- **Small-size live trading** ($5-10 bankroll): gated on explicit
  user approval per
  [.claude/rules/trading-safety.md](../../.claude/rules/trading-safety.md).
  This is the only way to *prove* strategy soundness.

## Inputs (strategy gate)

All strategy-validation values come from queries on
`data/poly_mm_live.db` or from `scripts/research/roundtrip_simulator.py`
output (which reads `poly_mm_live.db`). NEVER from `poly_mm_paper.db`.

- **net_pnl_cents** = output of round-trip simulator under the chosen
  survival model (mean of 200 trials). Maker rebates are recomputed
  via `calculate_maker_fee` to bypass the pre-2026-05-10 stored-fee
  bug.
- **yes_no_ratio** = `yes_fills_kept / no_fills_kept` from the
  simulator output. Both sides modeled equivalently (the simulator
  only drops yes_bid fills currently; future versions may drop
  no_bid too if Path B options are explored).

## Decision matrix — counterfactual on live, not paper

The decision matrix applies to **live counterfactual** outputs (or
eventually real live trading data), NOT to the paper bot's session
summaries. Paper-based metrics are operational signals, not strategy
evidence.

| net_pnl_cents (counterfactual on live) | yes_no_ratio (live, post-penalty sim, either side > 1) | Action |
|---|---|---|
| < -100 (worse than -$1) | > 1.4 | **KILL** — Path B. Fixes failed to rebalance flow. |
| < -100 | 1.0 - 1.4 | **KILL (urgent)** — Path B. Flow rebalanced but economics still negative; the underlying strategy is unprofitable independent of the asymmetry. |
| -100 to 0 | > 1.4 | **EXTEND** — close call. Try larger penalty or `tsc`-exclusion (path_b option 1). |
| -100 to 0 | 1.0 - 1.4 | **EXTEND** — economics borderline but flow looks healthy. |
| > 0 | > 1.4 | **EXTEND** — positive but flow unbalanced. Watch closely. |
| > 0 | 1.0 - 1.4 | **GREEN-LIGHT for small-size live trial** — both rebalanced and profitable in counterfactual. Live trial gated on explicit user approval. |

"Either side > 1.4×" means we check both `yes_no_ratio` and `1 / yes_no_ratio`
and use whichever is larger — the criterion is symmetric (over-quoting
EITHER side is unhealthy, even though current data shows YES-side
excess).

## Path B research artifacts (run in parallel)

[path_b_options.md](path_b_options.md) — initial analysis of three
alternative strategies to evaluate while paper-trading Path C:
1. Kalshi politics markets (flow symmetry hypothesis)
2. Same-platform taker role (capture the flow that adversely-selects us)
3. Market-type split (atc/asc/tsc/aec breakdown)

If KILL fires, the chosen Path B option becomes the next implementation
target. Don't kill without something to switch to.

## How to check

**Strategy gate (run against `poly_mm_live.db` only):** invoke the
round-trip simulator:

```bash
python scripts/research/roundtrip_simulator.py --trials 200 --seed 0
# Or per-prefix:
python scripts/research/roundtrip_simulator.py --filter-prefix tsc
```

Read the `net_pnl_c_mean` and `yes_fills_kept_mean / no_fills_kept_mean`
ratio per slice × model. Apply the decision matrix above.

**Operational shakedown checks (paper or live):** after each session
ends, the session summary will include per-side telemetry and the
headline `yes_bid : no_bid` ratio. These are NOT strategy gates; they
verify the instrumentation works.

```sql
-- Aggregate session telemetry — run against EITHER paper or live DB
-- depending on which you're shaking down. Strategy decisions must
-- still come from the round-trip simulator on poly_mm_live.db.
WITH last_snap AS (
  SELECT session_id, ticker, MAX(ts) AS max_ts FROM mm_snapshots
  WHERE ts >= '2026-05-10T00:00:00+00:00'  -- replace with window start
  GROUP BY session_id, ticker
)
SELECT
  SUM(s.realized_pnl + s.unrealized_pnl) AS total_pnl,
  SUM(s.total_fees) AS total_fees,
  SUM(s.realized_pnl + s.unrealized_pnl - s.total_fees) AS net_pnl
FROM mm_snapshots s
JOIN last_snap l ON s.session_id = l.session_id
                AND s.ticker = l.ticker
                AND s.ts = l.max_ts;

-- yes_no_ratio over a window (operational only — strategy gate uses simulator)
SELECT
  SUM(CASE WHEN side='yes_bid' THEN 1 ELSE 0 END) AS n_yes,
  SUM(CASE WHEN side='no_bid'  THEN 1 ELSE 0 END) AS n_no,
  CAST(SUM(CASE WHEN side='yes_bid' THEN 1 ELSE 0 END) AS REAL) /
    NULLIF(SUM(CASE WHEN side='no_bid' THEN 1 ELSE 0 END), 0) AS ratio
FROM mm_fills
WHERE filled_at >= '2026-05-10T00:00:00+00:00'
  AND side IN ('yes_bid', 'no_bid');
```

## Hard ceilings (override the matrix above)

These hard ceilings apply only to **live trading sessions**, never to
paper sessions:

1. **Net loss > $5 cumulative in a single live week** — bankroll-
   preservation trigger. $5 = 20% of the original $25 bankroll. This
   trigger fires from `poly_mm_live.db` only.
2. **Quote-disabled markets > 50% across live sessions** — if
   `should_disable_quoting` (paired_rate < 20%, see
   [src/mm/engine.py](../../src/mm/engine.py)) has fired on more
   than half the LIVE sessions, the strategy isn't reaching round-
   trips at all.

When a hard ceiling fires from live data, write the diagnostic to
`path_c_revision.md` and pause trading.

Paper-side quote-disabled rates and weekly P&L are operational data
points only — do not let them trigger a strategy KILL.

## Out of scope

- Live trading is not gated by this criterion alone. After Path C is
  judged CONTINUE, a separate gate (live-trading proposal with explicit
  human approval, see [trading-safety.md](../../.claude/rules/trading-safety.md))
  is required before any real-money decision.
- This criterion does NOT cover changes to the fix parameters mid-window
  (e.g., trying a 2c YES penalty). Parameter sweeps belong in a
  separate research phase, not this falsification test.
