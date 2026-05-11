# Handoff: Polymarket MM — Path C Validation + Asymmetry Diagnosis

**Generated:** 2026-05-11 by the session that executed Path C handoff v1
([handoff_2026-05-10_path_c_execution.md](handoff_2026-05-10_path_c_execution.md)).
**Phase:** Path C is implemented and the paper-trade validation window
is RUNNING. The work now shifts to (a) diagnosing the asymmetry more
rigorously, (b) deciding whether Path C succeeded.

## What just happened (one paragraph)

Path C handoff v1 prescribed 6 steps (cherry-pick fee fix, pair tracking,
asymmetric quote fixes, per-side telemetry, kill criterion, paper run).
All 6 are committed on `claude/heuristic-driscoll-fa5c58` (8 commits,
716 tests passing, pushed to origin). The paper bot is RUNNING (cron
+ initial manual launch on 2026-05-11 17:36 UTC). Critically, mid-session
we discovered that **paper simulation systematically over-fills (~10×)
and over-balances yes:no** — its fills are not predictive of live, so
paper-only validation is unreliable. A live-data counterfactual on the
326 historical fills shows the 1c YES penalty empirically improves
ratio and hold-to-settle P&L, but the practical round-trip impact
still needs simulation. Polymarket.us WebSocket exposes taker side
(`trade.maker.side` / `trade.taker.side`) — the engineering path to
fix paper.

## READ THESE FIRST (do not re-derive)

In order of importance:

1. **[CLAUDE.md](../../../CLAUDE.md)** — project conventions, TDD
   mandate, restart protocol. Updated 2026-05-11 with KILL_CRITERION
   reference + 45-55c scanner filter.

2. **[memory/MEMORY.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/MEMORY.md)**
   — all auto-memory. Critical reads:
   - [bartlett_ohara_paper.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/bartlett_ohara_paper.md) — project strategy base
   - [paper_vs_live_gap.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/paper_vs_live_gap.md) — **CRITICAL** — why paper validation is unreliable
   - [bot_termination_state.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/bot_termination_state.md)
   - [maker_fee_bug.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/maker_fee_bug.md) (FIXED)
   - [pair_tracking_broken.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/pair_tracking_broken.md) (FIXED)

3. **[data/research/yes_penalty_counterfactual.md](../yes_penalty_counterfactual.md)**
   — the live-data counterfactual that's the closest thing to real
   validation we have. The penalty empirically helps in all three
   survival models.

4. **[data/research/KILL_CRITERION.md](../KILL_CRITERION.md)** —
   updated 2026-05-11: paper alone is insufficient; strategy decisions
   require live counterfactual or live trading data.

5. **[data/research/fill_asymmetry_diagnosis.md](../fill_asymmetry_diagnosis.md)**
   — the foundational diagnosis from the prior session (H2 confirmed:
   YES-seller taker flow on Polymarket sports).

6. **[data/research/path_b_options.md](../path_b_options.md)** —
   alternatives if Path C kills.

## Current state of the repo

### Branch + main

- **Active branch:** `claude/heuristic-driscoll-fa5c58` (pushed to
  origin). 8 commits ahead of main.
- **Main:** stays at `52ff385`. **Do not merge to main** until the
  user explicitly says "merge". (User instruction 2026-05-11:
  "do not merge to main yet, wait until we verified and researched
  and diagnosed asymmetry".)
- **Worktree path:** `/Users/openclaw/polymarket-arb/.claude/worktrees/heuristic-driscoll-fa5c58`
- **Tests:** 716 passing (656 baseline + 60 new).

### What's running

- **Paper bot** (`scripts/poly_paper_mm.py`) — PID 52070 (verify
  with `pgrep -fl poly_paper_mm`). Session
  `20260511-173604-cdfd61`. 5 markets, all `tsc` (totals). 24h
  duration → exits ~2026-05-12 17:36 UTC. Log:
  `data/poly_mm_paper_20260511_173604.log`.
- **Cron schedule** (active): `0 8,10,12,14,16,18 * * *` runs the
  scanner in `--smart-run --paper` mode, hot-adds to running bot
  or launches fresh. Log: `data/scanner_cron.log`. View with
  `crontab -l | grep paper_mm`.

### Path C fixes — all committed

| Commit | What |
|---|---|
| `a28186e` | Engine fee DI |
| `809aff4` | Pair tracking persistence |
| `da72848` | YES penalty + round() + scanner 45-55c |
| `9fb2b2f` | Per-side telemetry + Discord ratio |
| `eef3fd1` → `63a46a3` | KILL_CRITERION (with mid-session update) |
| `fcbe001` | Path B research doc |
| `77a3b79` | Scanner subprocess paths absolute (cron compatibility) |
| `791278f` | YES penalty counterfactual on live |

### Critical findings from this session

1. **Paper-vs-live fill gap** ([paper_vs_live_gap.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/paper_vs_live_gap.md)):
   - Paper: 32.8 fills/market, yes:no = 0.99
   - Live: 2.96 fills/market, yes:no = 1.79 (or 4.5× excluding 04-02 anomaly)
   - Root cause: [drain_queue()](../../../src/mm/engine.py:199-218)
     ignores taker side; counts both yes-buyers and yes-sellers as
     drainable for both YES_bid and NO_bid simulations.

2. **YES penalty counterfactual on 326 live fills** ([yes_penalty_counterfactual.md](../yes_penalty_counterfactual.md)):
   - Under three survival models, penalty cuts yes_bid fills by
     41-75%, lands yes:no ratio at 0.44 (over-corrected) /
     0.69 / 1.05 (target), and improves hold-to-settle P&L from
     baseline −$13.49 to −$0.57 to −$9.32.
   - The penalty is empirically supported on real data.
   - **But hold-to-settle ≠ round-trip.** Bot realized −$2.92, not
     −$13.49 — round-trip exits recovered most of the settlement loss.
     The penalty's PRACTICAL impact requires a round-trip simulator
     to nail down.

3. **Polymarket.us WebSocket exposes taker side**: `p.ws.markets.subscribe_trades([slugs])`
   emits `Trade` messages with `trade.maker.side`, `trade.maker.intent`,
   `trade.taker.side`, `trade.taker.intent`. Schema at
   [polymarket_us/websocket/types.py](file:///Users/openclaw/miniconda3/lib/python3.13/site-packages/polymarket_us/websocket/types.py).
   This is the proper path to make paper trustworthy again.

## The 4-step plan for next session (prioritized)

### Step 1 — Per-prefix counterfactual breakdown (~30 min)

**What:** Extend [yes_penalty_counterfactual.py](../../scripts/research/yes_penalty_counterfactual.py)
to break out by slug prefix (`tsc` / `asc` / `aec` / `atc`).

**Why:** `tsc` has 3.67× imbalance in production, `asc` has 1.39×
(already in the Path C target window). If penalty over-corrects
`asc` while fixing `tsc`, the right design is a **differential
penalty by marketType** — not a flat 1c.

**Approach:** Modify the script to slice the 326 fills by prefix
(first 3 chars of ticker) and run all three survival models per
prefix. Write findings to a new section of [yes_penalty_counterfactual.md](../yes_penalty_counterfactual.md)
(append, don't replace).

**Acceptance:** new per-prefix table showing pre- and post-penalty
yes:no ratio and hold-to-settle P&L per `tsc`/`asc`/`aec`. Recommendation
on whether to: (a) keep flat 1c, (b) make penalty marketType-aware,
or (c) drop one prefix from scanner entirely.

### Step 2 — Round-trip simulator (~half day)

**What:** Build a counterfactual that respects the bot's actual
round-trip dynamics, not hold-to-settle.

**Why:** The real metric is round-trip P&L. Hold-to-settle is the
worst-case upper bound. The bot realized −$2.92 vs hold-to-settle
−$13.49 — the round-trip exit recovered $10.57. We need to predict
how the penalty changes round-trip P&L specifically.

**Approach:**
- Take the 326 live fills + their snapshots
- Sequence them by `filled_at`
- Apply survival probability to drop yes_bid fills
- Track inventory queue: yes_queue / no_queue with fill_id
- Use `pair_off_inventory` to match opposing fills
- For unpaired inventory at game start, apply progressive exit ladder
  (see [DEFAULT_EXIT_LADDER](../../../src/mm/state.py))
- Sum: pair_pnl from pair-offs + exit_pnl from forced exits − fees

This won't be perfect (timing is approximate, exit prices estimated)
but should give a much better P&L estimate than hold-to-settle.

**Acceptance:** script + report estimating post-penalty round-trip
P&L for each of the three survival models. If predicted P&L is
positive → strong evidence Path C works; if negative → may need
larger penalty, or drop `tsc`, or other.

### Step 3 — Operational check on the in-flight paper session

**When:** ~2026-05-12 17:36 UTC, when the bot's 24h duration ends.
**What:** Verify the new instrumentation produced the right output.

**Checklist:**
- `data/poly_mm_paper.db` — does mm_fills have rows from session
  `20260511-173604-cdfd61` with **non-NULL pair_id and pair_pnl**?
  (`SELECT pair_id, pair_pnl FROM mm_fills WHERE session_id='20260511-173604-cdfd61' LIMIT 10`)
- Discord — did the session-end message include `yes=N no=N ratio=X.XX`?
- Session summary markdown at `.claude/sessions/poly-<session_id>.md`
  — does it have a "Per-Side Telemetry" section with the metric table?
- `data/scanner_cron.log` — did the 12:00 / 14:00 / 16:00 / 18:00
  cron fires happen cleanly? Any errors?
- `data/poly_mm_paper.db` snapshot of fills — what's the yes:no
  ratio on the live paper session? (Expectation: paper will show
  ~1.0 even though real live behavior on these markets would be
  ~3× — see paper_vs_live_gap.md for why.)

### Step 4 — WebSocket taker-side collector (~1-2 days, separate session)

**What:** Build a collector that subscribes to
`p.ws.markets.subscribe_trades(active_slugs)` and writes each trade
to a new DB table, persisting `maker.side`, `maker.intent`, `taker.side`,
`taker.intent`, `price`, `quantity`, `tradeTime`, `marketSlug`.

**Why:** Once we have aggressor-aware trade history, we can:
1. Fix `drain_queue()` to count only trades whose `taker.side` matches
   our bid's intent
2. Rerun the round-trip simulator with aggressor-aware queue dynamics
3. Use the same data to compute VPIN (per Bartlett & O'Hara Section 6)
   as a real-time toxicity gate

**Approach:**
- New script `scripts/trade_tape_collector.py` (long-running)
- New DB table `mm_trade_tape` (slug, price, qty, maker_side,
  maker_intent, taker_side, taker_intent, ts, recorded_at)
- Subscribes to all `data/poly_active_slugs.json` markets, refreshes
  subscription when slugs change
- Reconnects on disconnect; backfills aren't possible (WS is real-time
  only)
- Cron entry to start the collector on boot

**Don't merge to main until:** Steps 1, 2, and 4 are all done AND the
results pass the kill criterion.

## Constraints (from CLAUDE.md + user instructions)

1. **TDD mandatory** for any code in Steps 2 and 4.
2. **Full test suite** after each code-change step:
   `python -m pytest tests/ -q` (716 passing baseline).
3. **Never push to main** — user blocked this 2026-05-11. Branch
   stays separate; user will say when to merge.
4. **Restart bot protocol** if any change to `src/mm/*.py`,
   `src/poly_client.py`, `scripts/poly_*_mm.py`, or
   `scripts/monitor_drain.py`:
   - commit → `pkill -9 -f poly_paper_mm` → restart (or let cron
     re-launch on next fire) → verify new code in startup log.
   - Note: the running PID 52070 is on commit `791278f`. If you
     change anything before next cron fire, kill + relaunch.
5. **Don't claim "paper validation passed" based on paper P&L
   absolutes.** Paper P&L is unreliable per
   [paper_vs_live_gap.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/paper_vs_live_gap.md).
   Use paper for operational signals; use counterfactual on live
   data for strategy decisions.

## If you discover the plan is wrong

The empirical-first principle applies most strongly when data
contradicts the plan. If during execution:
- The per-prefix counterfactual shows penalty hurts `asc` more
  than helps `tsc` → propose differential penalty design
- The round-trip simulator shows penalty makes round-trip P&L
  worse → propose Path B Option 1 (asc-only) or Option 4 (NO-only)
- The paper session doesn't write pair_pnl → there's a bug in our
  Step 2 commit; debug and fix
- The WebSocket collector reveals different aggressor distribution
  than expected → update the strategy base accordingly

Pause and write a brief diagnostic to `data/research/path_c_revision.md`
(or `path_c_revision_v2.md` if v1 exists) before proceeding.

## Success criteria for the next batch

- Step 1 done: per-prefix counterfactual committed with clear
  recommendation
- Step 2 done: round-trip simulator with TDD coverage; report on
  post-penalty round-trip P&L per survival model
- Step 3 done: in-flight paper session verified clean OR bug found
  and fixed
- (Step 4 deferred to its own dedicated session)
- MEMORY.md updated if new persistent learnings emerge
- All commits pushed to `origin/claude/heuristic-driscoll-fa5c58`

## How to start

1. Read this handoff + the "READ THESE FIRST" files
2. Check paper bot status: `pgrep -fl poly_paper_mm` (should be
   running unless its 24h duration ended) + `tail -20
   data/scanner_cron.log` for recent cron activity
3. Begin with Step 1 (per-prefix counterfactual — fastest)
4. Then decide based on results whether Step 2 or Step 3 is next
