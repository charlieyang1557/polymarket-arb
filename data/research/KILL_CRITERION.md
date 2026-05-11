# Kill Criterion — Path C → Path B Trigger

**Status:** Active as of 2026-05-10 (after Steps 1-4 of Path C handoff).
**Owner:** Auto-checked at end of every paper-trading session.

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

**4 calendar weeks of paper trading**, beginning the first session
after Steps 1-5 of the handoff are merged. Re-evaluation at any
intermediate point is allowed only to *pause* the test (e.g., bankroll
exhaustion); the criterion below requires 4 full weeks of data.

Sessions must use `scripts/poly_paper_mm.py` (paper trading) — not
live — until this criterion is decided.

## Inputs

All values come from queries on `data/poly_mm_paper.db` (or wherever
`--db-path` points) aggregated over all sessions in the window:

- **net_pnl_cents** = SUM(realized_pnl + unrealized_pnl) at last
  snapshot per (session_id, ticker), minus session fees. Polymarket
  rebates are negative fees (credit), so net should already be correct
  after the Step 1 fee DI fix.
- **yes_no_ratio** = SUM(yes_bid fill rows) / SUM(no_bid fill rows)
  across all sessions in the window.

## Decision matrix

| net_pnl_cents | yes_no_ratio (or its inverse, whichever > 1) | Action |
|---|---|---|
| < -100 (worse than -$1) | > 1.4 | **KILL** — Path B. Fixes failed to rebalance flow. |
| < -100 | 1.0 - 1.4 | **KILL (urgent)** — Path B. Flow rebalanced but economics still negative; the underlying strategy is unprofitable independent of the asymmetry. |
| -100 to 0 | > 1.4 | **EXTEND 2 weeks** — close call. Flow still asymmetric; one more cycle may clarify whether penalty needs increasing. |
| -100 to 0 | 1.0 - 1.4 | **EXTEND 2 weeks** — economics borderline but flow looks healthy. |
| > 0 | > 1.4 | **EXTEND 2 weeks** — positive but flow unbalanced. Watch closely. |
| > 0 | 1.0 - 1.4 | **CONTINUE** — both rebalanced and profitable. Continue paper trading another 2 weeks for confirmation before any live discussion. |

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

## How to check (operational)

After each paper session ends, the session summary will already include
per-side telemetry and the headline `yes_bid : no_bid` ratio (Step 4).
To check aggregate over the window:

```sql
-- net_pnl_cents over a window
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

-- yes_no_ratio over a window
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

Two unconditional KILL conditions short-circuit the 4-week window:

1. **Net loss > $5 cumulative in a single week** — bankroll-preservation
   trigger. $5 = 20% of the original $25 bankroll; this is more than the
   $2.92 prior loss in a much shorter span.
2. **Quote-disabled markets > 50%** — if `should_disable_quoting`
   (paired_rate < 20%, see [src/mm/engine.py](../../src/mm/engine.py))
   has fired on more than half the sessions, the strategy isn't
   reaching round-trips at all — the YES penalty cut fills too far.

When a hard ceiling fires, write the diagnostic to `path_c_revision.md`
and pause trading.

## Out of scope

- Live trading is not gated by this criterion alone. After Path C is
  judged CONTINUE, a separate gate (live-trading proposal with explicit
  human approval, see [trading-safety.md](../../.claude/rules/trading-safety.md))
  is required before any real-money decision.
- This criterion does NOT cover changes to the fix parameters mid-window
  (e.g., trying a 2c YES penalty). Parameter sweeps belong in a
  separate research phase, not this falsification test.
