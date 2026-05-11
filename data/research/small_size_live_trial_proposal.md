# Small-Size Live Trial — Proposal (awaits user approval)

**Generated:** 2026-05-11.
**Status:** **NOT EXECUTED.** This document is a proposal for the user
to review and approve. Per
[.claude/rules/trading-safety.md](../../.claude/rules/trading-safety.md),
real money trading requires explicit user approval; this AI agent
cannot launch live trading on its own initiative.

## Goal

Generate **ground-truth** live trading data (per
[memory/feedback_live_is_ground_truth.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/feedback_live_is_ground_truth.md))
to validate the round-trip simulator's predictions and calibrate
strategy decisions.

## What's already validated (from non-trading work, fully complete)

- ✅ Round-trip simulator (762 tests passing, commit
  [8b93873](https://github.com/charlieyang1557/polymarket-arb/commit/8b93873))
- ✅ Differential penalty by marketType (commit
  [d15470e](https://github.com/charlieyang1557/polymarket-arb/commit/d15470e))
- ✅ WebSocket trade tape collector (commit
  [6b09616](https://github.com/charlieyang1557/polymarket-arb/commit/6b09616))
- ✅ All Path C fixes (fee DI, pair tracking, per-side telemetry, kill
  condition tracker)

## The decision the user has to make

Pick ONE option, then approve. All options are gated on:

- $5 max bankroll (500c) — `.claude/rules/trading-safety.md` daily loss
  limit
- Manual launch (not via cron / auto)
- Restrict to 1-3 markets max (small surface)
- Use `--no-confirm` only if running headless; otherwise keep prompt
- Set `--duration` to limit exposure window

### Option A — Validate CURRENT Path C (flat 1c penalty)

**Tests:** "does the current Path C state — flat 1c penalty + all
fixes from commits a28186e, 809aff4, da72848, 9fb2b2f — actually give
net positive P&L on small live?"

**Simulator prediction:** +$0.63 net per 326-fill sample (≈ 21
sessions). For one 24h session: roughly +$0.03 expected, std ~$0.10.
**This trial is statistically underpowered** to distinguish from
zero. Real value: rule out catastrophic failures (e.g., does the
penalty implementation actually fire correctly? do pair_pnl rows
persist? does Discord emit?).

**Launch command:**

```bash
cd /Users/openclaw/polymarket-arb
python scripts/poly_live_mm.py \
    --slugs <SLUG1>,<SLUG2> \
    --capital 500 \
    --size 1 \
    --interval 10 \
    --duration 21600   # 6 hours
```

Pick `<SLUG1>,<SLUG2>` from `data/poly_active_slugs.json` (or run
scanner first). Recommend `tsc-mlb-*` markets (the prefix the
penalty was designed for).

**Pros:** No new code changes. Lowest approval gate. Validates
operational pipeline.

**Cons:** Tests a known-suboptimal config (flat 1c). The simulator
already says differential is +$1.85 better. Trial result is unlikely
to give actionable strategy signal.

### Option B — Test the recommended differential (tsc-only 1c)

**Tests:** "does the simulator's +$2.48 differential prediction hold
live?"

**Required code changes (need separate approval per
trading-safety.md):**
- `src/mm/state.py` `skewed_quotes()`: accept `yes_penalty_map` like
  `{"tsc": 1}` instead of scalar `yes_penalty=1`. Default to flat
  behavior for backward compat.
- `scripts/poly_live_mm.py`: pass the map to engine; CLI flag
  `--penalty-map "tsc:1"` (or env var).
- Tests covering both flat and differential modes.
- Re-run round-trip simulator post-implementation to verify the
  +$2.48 prediction reproduces with the actual code path.

**Simulator prediction:** +$2.48 net for 326-fill sample under base
survival model.

**Launch command:** same as A, but with `--penalty-map "tsc:1"`.

**Pros:** Tests the BEST predicted config. Builds the production-
ready differential mechanism. Strongest signal-per-dollar of trial
data.

**Cons:** Two approval gates (code change + live trial). ~1 session
of additional code+test work before launch.

### Option C — A/B baseline vs flat-1c

**Tests:** "what's the live delta between baseline (no penalty) and
flat-1c?"

**Approach:** Run two parallel bots on DIFFERENT markets:
- Bot A: no penalty (set `yes_penalty=0`), 1-2 markets
- Bot B: flat 1c (current Path C), 1-2 markets

Match market selection on prefix to minimize confounds.

**Pros:** Direct A/B on real money. Maximum information per dollar.

**Cons:** Requires CLI flag for penalty=0; doubles bankroll need;
parallel processes are operationally complex. Best deferred until
after trade tape collector accumulates a few days of data.

### Option D — Trade-tape-only (no live trading)

**Tests:** Same hypothesis as B, but using the WebSocket trade tape
to enrich the simulator instead of going live.

**Approach:**
1. Run [scripts/trade_tape_collector.py](../../scripts/trade_tape_collector.py)
   for 2-3 weeks (no real money).
2. Use the aggressor-aware trade data to fix `drain_queue()` and
   rerun the round-trip simulator on existing live fills.
3. If the simulator's predictions hold under aggressor-aware
   dynamics, then approve live trial.

**Pros:** No real money risk during data collection. Re-uses existing
live history (326 fills) for richer counterfactual.

**Cons:** 2-3 weeks of collection before any new strategy signal.
Cannot validate the differential penalty itself without going live;
just calibrates the simulator.

## Recommendation

**Option D, then Option B.** Rationale:

1. Trade tape collector is already built and tested (commit
   [6b09616](https://github.com/charlieyang1557/polymarket-arb/commit/6b09616));
   running it costs nothing. Setting it up via pm2 is one line.
2. After 1-2 weeks of trade tape data, the round-trip simulator's
   predictions become much more trustworthy (real queue dynamics
   instead of last-snapshot BBO heuristics).
3. THEN run Option B with strong simulator-backed conviction. The
   +$2.48 prediction may shift up or down with aggressor-aware data,
   and we want to commit live capital based on the BEST prediction.

**If the user wants live data sooner** (e.g., to validate the
simulator's absolute level against realized P&L), Option A is fine
as a smoke-test — just don't draw strategy conclusions from $5 of
P&L noise.

## Pre-launch checklist (when user approves any option)

- [ ] Confirm `.env` has valid POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY
- [ ] Confirm `data/poly_mm_live.db` is writable and current bot uses
      `--db-path` pointing there
- [ ] Verify capital limit: `--capital 500` (or chosen amount)
- [ ] Verify max-position cap fires correctly (test in --dry-run first)
- [ ] Confirm Discord webhook still routes to user's channel
- [ ] Pre-stage the slugs (run scanner or pick manually); validate
      they're not currently live games (pre-game only per CLAUDE.md)
- [ ] Set `--duration` so the bot exits before user goes offline
- [ ] (Option B/C only) merge the code change to main, verify all
      tests pass, restart cleanly
- [ ] Optional: start trade tape collector in parallel (
      `pm2 start scripts/trade_tape_collector.py --name trade-tape`)
      so the trial generates aggressor-aware data too

## Stop conditions (auto)

Already in place via `src/mm/risk.py`:
- Daily loss limit -$5 (500c) → FULL_STOP
- Consecutive 3 losses → PAUSE_30MIN
- Per-market -$10 cumulative → EXIT_MARKET
- Quote-disabled at <20% paired_rate after 3+ fills → SOFT_CLOSE only

## Stop conditions (user-driven)

- ANY unexpected behavior in the first 30 min → kill bot, review logs
- Daily loss > $3 → kill regardless of risk-layer state
- Confusion about what's happening on the order book → kill, review

## What to monitor during the trial

Tail these in parallel windows:

```bash
# Live bot output
tail -f /Users/openclaw/polymarket-arb/data/poly_mm_live_<session>.log

# Order activity
sqlite3 /Users/openclaw/polymarket-arb/data/poly_mm_live.db \
  "SELECT * FROM mm_fills WHERE session_id='<session>' ORDER BY filled_at DESC LIMIT 10"

# Trade tape (if running)
sqlite3 /Users/openclaw/polymarket-arb/data/poly_trade_tape.db \
  "SELECT market_slug, COUNT(*) FROM mm_trade_tape WHERE recorded_at > datetime('now', '-1 hour') GROUP BY market_slug"
```

## Post-trial deliverables

After the bot exits (clean or stopped):

1. Pull session summary from `.claude/sessions/poly-<session>.md`
2. Aggregate fills, ratio, realized P&L from `poly_mm_live.db`
3. Compare realized vs simulator prediction:
   - For Option A: realized vs simulator's flat-1c prediction
     (+$0.63 / 21 sessions = +$0.03 per 24h session in expectation)
   - For Option B: realized vs differential prediction (+$2.48 / 21 =
     +$0.12 per 24h session)
4. Write findings to `data/research/live_trial_<date>.md`
5. Decide next step: scale up bankroll, change config, or abort.
