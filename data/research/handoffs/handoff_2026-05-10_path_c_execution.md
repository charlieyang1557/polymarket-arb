# Handoff: Polymarket MM Strategy — Path C Execution

You're picking up a Polymarket sports market-making project (`polymarket-arb`) after a research phase. The prior session diagnosed the strategy as structurally compromised but worth one more shot with concrete fixes. Path C is **"apply cheap fixes + 4-week paper validation, with parallel research into alternative strategies."** Your job is to execute the 6-step plan below.

## Read these first (do not re-derive)

- [CLAUDE.md](../../CLAUDE.md) — project conventions, **TDD mandate**, risk layers, restart protocol
- [data/research/STRATEGY_DECISION.md](STRATEGY_DECISION.md) — synthesis of why the bot was losing (~$2.92 over 34 sessions); decision matrix
- [data/research/fill_asymmetry_diagnosis.md](fill_asymmetry_diagnosis.md) — root cause of 209 yes_bid vs 117 no_bid fills (H2 confirmed: Polymarket sports has structural taker-flow asymmetry; YES-sellers dominate). Contains exact code prescriptions for Fixes 1-3.
- [data/research/settlement_pnl_report.md](settlement_pnl_report.md) — counterfactual hold-to-settle (failed: would have lost $13.49)
- Memory files at `/Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/MEMORY.md` — bot termination state, two side-bugs, hold-to-settle thesis failure, "check production P&L first" feedback

## Current state of the repo

- **Branch:** `claude/charming-mcclintock-caff3f` (pushed to origin)
- **Bot:** Not running. Terminated ~2026-05-03 by user after losing money.
- **Tests:** 740 passing as of last full run.
- **Fee-bug fix:** Ready in a separate isolated worktree at `/Users/openclaw/polymarket-arb/.claude/worktrees/agent-aa22b2c608e45fafd`, branch `worktree-agent-aa22b2c608e45fafd`, commit `8c79c1f`. 14 new tests, 0 regressions. **Important nuance:** the fee bug only affected `poly_paper_mm.py` (paper trading); `poly_live_mm.py` was already using `calculate_maker_fee()` correctly inline. So merging this fix prevents future regression but doesn't recover any past money.

## The 6-step plan

### Step 1 — Merge the fee fix

```bash
git fetch
# The agent's commit on the isolated worktree branch
git cherry-pick 8c79c1f
python -m pytest tests/ -q  # confirm 754 pass (was 740 → +14 new)
```

If cherry-pick conflicts, the change touches only `src/mm/engine.py` (12 lines), `scripts/poly_paper_mm.py` (5 lines), and adds `tests/test_engine_fee_injection.py` (200 lines new). Resolve in favor of incoming.

Commit message preserved from the agent's original.

### Step 2 — Fix pair_pnl tracking (TDD)

All 326 historical fills have `pair_id=0` and `pair_pnl=NULL`. The bot's pair-off logic isn't writing back to mm_fills.

**Investigate:**
- `pair_off_inventory()` in `src/mm/engine.py` (called when a fill reduces inventory)
- `db.update_fill()` or similar — likely missing call site
- The pair detection logic may be running but not persisting

**TDD:**
- New file `tests/test_pair_off_persistence.py`
- Tests: synthetic 2-fill sequence (yes_bid at 50c size=2, then offsetting no_bid at 48c size=2) should produce non-null `pair_id` and `pair_pnl` on the second fill (and possibly back-update the first)
- Edge cases: partial offset (yes_bid size=2, no_bid size=1 → 1 contract paired), multi-step pair-offs

**Acceptance:** Running the bot for 1 session with synthetic fills produces non-null pair_pnl in mm_fills.

### Step 3 — Apply Fixes 1-3 from the asymmetry diagnosis

Reference: [fill_asymmetry_diagnosis.md](fill_asymmetry_diagnosis.md) "Recommended fixes" section has exact code prescriptions.

- **Fix 1** (biggest impact): 1¢ YES-side adverse-selection penalty in `skewed_quotes()` at `src/mm/state.py:84`. Quote: `yes_price = max(1, math.floor(fair - half_spread - quote_offset - skew_raw - 1))` — note the `- 1`.
- **Fix 2** (free): Replace `math.floor()` with `round()` at `src/mm/state.py:84-85` for symmetric rounding. Removes 10-15% of rounding-bias contribution.
- **Fix 3**: Tighten scanner midpoint filter from 35-65c to 45-55c in `scripts/poly_daily_scan.py` (search for the midpoint filter in `scan_today_sports`).

**TDD:**
- New file `tests/test_skewed_quotes_yes_penalty.py` — verify YES bid is 1c lower than symmetric, NO bid unchanged
- Update `tests/test_mm_state.py` for any tests that asserted exact floor() behavior
- Update `tests/test_poly_daily_scan.py` for the new midpoint range

**Note:** Fix 1 changes strategy behavior. Document in commit message that this is an empirically-derived asymmetric quote, and reference the diagnosis report.

### Step 4 — Add per-side telemetry

Currently session summaries report aggregate P&L. Add separate per-side stats:

- yes_bid: n_fills, contracts, mean_half_spread_cents, total_pnl_cents, win_rate
- no_bid: same
- yes_bid_to_no_bid_fill_ratio (key Path-A success metric)

Likely edit `scripts/poly_paper_mm.py` session-end summary and `scripts/poly_live_mm.py` if it has its own. Look at how `tests/test_session_summary.py` exercises this and extend.

Also surface yes/no ratio in Discord notifications at session end so it's visible at a glance.

### Step 5 — Define kill criterion

Create `data/research/KILL_CRITERION.md` with explicit Path C → Path B trigger:

> After 4 weeks of paper trading post-fixes:
> - If session-aggregate net P&L is negative ($-margin worse than $-1) AND yes_bid:no_bid fill ratio > 1.4× either side → move to Path B
> - If fill ratio is rebalanced (1.0–1.4×) but P&L still negative → Path B is more urgent (means quoting fix worked but underlying economics still bad)
> - If both rebalanced AND P&L positive → continue paper trading another 2 weeks for confirmation before any live discussion

Reference this file from CLAUDE.md's risk-management section. Update CLAUDE.md.

### Step 6 — Start paper trade + parallel B research

**Operationally:**
- Start `scripts/poly_paper_mm.py` (NOT live) with current scanner + fixes applied
- Monitor daily: check per-side telemetry, session P&L
- Bot restart protocol applies — verify new code is loaded after every src/mm/* change

**B-research (no implementation, just analysis) — document in `data/research/path_b_options.md`:**
- **Kalshi politics markets** — investigate flow symmetry vs Polymarket sports. Use Kalshi's `kalshi_daily_scan.py` and pull historical trade data. Is there asymmetric taker flow on politics markets?
- **Same-platform taker role** — quantify the theoretical edge of being the YES-buyer on the same flow we're being adversely-selected on. If takers sell YES at 49c when fair is 50c, buying at 49c (paying taker fees) captures 1c minus fees. Profitable?
- **Market-type split** — Phase 1.2 only stratified by category=sports. Split by `marketType` (atc=alt-spread, asc=alt-spread+, tsc=totals, aec=moneyline) and see if any subset has symmetric flow.

## Constraints (from CLAUDE.md)

1. **TDD mandatory** for all code in steps 1-4. Tests fail first, then implementation, then tests pass.
2. **Run full test suite** after each step: `python -m pytest tests/ -q`
3. **Never commit to main.** Stay on `claude/charming-mcclintock-caff3f` or branch from it.
4. **Restart bot protocol** after any change to `src/mm/*.py`, `src/poly_client.py`, `scripts/poly_*_mm.py`, or `scripts/monitor_drain.py`:
   - commit → kill running bot (none running now, but check) → restart with same config → verify startup log shows new code version
5. **Use memory** at `/Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/` for any new persistent learnings (e.g., if pair_pnl fix reveals a deeper architecture issue, save a `project` memory).
6. **Do not modify** existing research artifacts in `data/research/`. They are project history. New analysis goes in new files.

## Success criteria for this work batch

- Steps 1-5 fully committed with passing tests; full suite ≥ 754 passing
- Paper bot running with new fixes (Step 6.a)
- `data/research/path_b_options.md` exists with initial findings (Step 6.b)
- `data/research/KILL_CRITERION.md` exists and referenced from CLAUDE.md
- Branch pushed to remote
- MEMORY.md updated if any new persistent learnings emerged

## How to start

1. Use `TodoWrite` to track the 6 steps as todos
2. Read CLAUDE.md and STRATEGY_DECISION.md first to ground yourself
3. Begin with Step 1 (cherry-pick the fee fix)
4. Before each subsequent step, re-read the referenced files to verify current code state — don't assume

## If you discover the plan is wrong

Path C is a falsifiable hypothesis. If during execution you find evidence that the plan should change — e.g., pair_pnl tracking turns out to be a much deeper issue, or Fix 1 has unexpected side effects — **pause and write a brief diagnostic to `data/research/path_c_revision.md` before proceeding**. Don't silently deviate. The empirical-first principle from CLAUDE.md applies most strongly when the data contradicts the plan.

Good luck. The work to date is in good shape; the question now is whether path C's fixes change the empirics enough to keep the strategy alive, or confirm we need Path B.
