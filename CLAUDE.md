# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kalshi sports market making bot. Quotes both sides of pre-game sports markets (NCAA spreads/totals), captures the bid-ask spread, and exits all positions before game start.

- **Platform**: Kalshi (CFTC-regulated, RSA-PSS auth)
- **Strategy**: Pre-game market making with OBI microprice, continuous inventory skew, dynamic volatility-based spread
- **Pre-game only**: Exit all positions when live game detected (>50 trades/5min)
- **Current phase**: Paper trading (preparing for $25 live bankroll)

## Commands

```bash
# Paper trading (main entry point)
python scripts/paper_mm.py --tickers TICKER1,TICKER2 --duration 86400

# Scanner
python scripts/kalshi_daily_scan.py --max-markets 15           # scan only, Discord summary
python scripts/kalshi_daily_scan.py --run --max-markets 15     # scan + auto-launch bot

# Pre-flight (runs before cron launch)
bash scripts/preflight.sh

# Queue drain monitor
python scripts/monitor_drain.py

# Tests (full MM suite)
python -m pytest tests/test_mm_*.py tests/test_*skew*.py tests/test_*spread*.py tests/test_*obi*.py tests/test_pregame*.py tests/test_silent*.py tests/test_monitor*.py tests/test_inventory*.py tests/test_daily_scan.py tests/test_session_summary.py -q
```

## Architecture

```
scripts/paper_mm.py              → Main entry point (paper trading)
scripts/kalshi_daily_scan.py     → Market scanner with rank-based scoring
scripts/preflight.sh             → Pre-flight checks (git clean, tests, API, Discord)
scripts/monitor_drain.py         → Queue position monitor

src/kalshi_client.py             → Kalshi API (RSA-PSS auth, raw HTTP)
src/mm/engine.py                 → Market making engine (10s tick loop)
src/mm/state.py                  → MarketState, OBI microprice, skewed_quotes, dynamic_spread
src/mm/risk.py                   → 4-layer risk management (L1-L4)
src/mm/db.py                     → SQLite persistence (fills, orders, snapshots)
```

Data flow: Scanner selects markets → Engine quotes both sides → Fills tracked via queue drain → Inventory skew adjusts → Pre-game exit on live game detection.

## Risk Management

```
Layer 1: Per-order validation
  - Fat-finger: price within ±10% of midpoint
  - Max size: 5 contracts per order (paper)

Layer 2: Inventory management
  - Continuous skew: gamma=0.5c per contract (always active)
  - Single-side cap: stop quoting side that increases inv past 10
  - Profitability floor: reduce skew if same-tick round-trip unprofitable
  - Time-based: AGGRESS_FLATTEN at 2h, FORCE_CLOSE at 4h unhedged
  - Emergency: AGGRESS_FLATTEN at inv>10, STOP_AND_FLATTEN at inv>20

Layer 3: Session-level P&L
  - Daily loss limit: -500c ($5) → FULL_STOP
  - Consecutive loss pause: 3 losses → PAUSE_30MIN (resets after)
  - Drawdown gate: FULL_STOP only when session is net negative (current < 0)
  - Per-market exit: -1000c cumulative → EXIT_MARKET

Layer 4: System checks
  - Price jump detection: 3c live / 5c pre-game in 65s → PAUSE_60S
  - Crossed book → SKIP_TICK
  - API disconnect >30s → CANCEL_ALL
  - DB errors ≥10 → FULL_STOP
  - Pre-game exit on live game detection (>50 trades/5min)
  - Time-based exit from game schedule: SOFT_CLOSE at 15min, EXIT_MARKET at game time
  - Soft-close at trade freq 30-50 (reduce-only mode)
  - Session drift >10c from initial midpoint → EXIT_MARKET (pricing model invalid)
  - Auto-deactivate after 30 consecutive empty orderbook ticks
```

## Scanner Filters

```
Pre-filters (binary pass/fail):
  - net_spread > 0 and <= 8, where net_spread = market_spread - 2 * ceil(0.0175 * P * (1-P) * 100). This is gross spread minus estimated round-trip maker fees.
  - spread < 15
  - midpoint 35c - 65c (filters alt-lines/blowout bets with toxic adverse selection)
  - symmetry 0.2 - 5.0
  - L1 queue depth < 20,000
  - trades_per_hour >= 10
  - hours_to_expiration > 1
  - Both sides must have depth > 0

Ranking: rank-based composite (no magic weights)
  - rank by net_spread (descending)
  - rank by max(yes_depth, no_depth) (ascending)
  - rank by trades_per_hour (descending)
  - composite = average of three ranks
  - ties: average ranking method
```

## Design Decisions

- **Cross-tick losses are stop-losses, not bugs**: Negative gross round-trips (YES+NO > 100c) are natural stop-losses when the market moves between fills. The alternative (refusing to bid at market price) leads to holding unhedged inventory into settlement, which is far worse. The profitability floor in `skewed_quotes()` only applies to same-tick quote pairs. Do NOT implement cost-basis-aware quoting.

- **Pre-game only**: We do NOT trade during live games. Our 10-second polling cannot compete with sub-second HFT during live events.

- **Soft-close deadlock is acceptable**: If bot gets stuck in soft-close with inv=±2, the bounded loss (~$2) is acceptable. Do not implement taker aggress to break deadlock at current scale.

- **Spread P&L vs inventory P&L**: Only spread capture is sustainable. Directional profits from inventory appreciation are luck, not edge. Session summaries decompose P&L to track this.

- **Sweep order windfalls are bonuses, not edge**: Occasionally fills occur at extreme prices (e.g., NO@76c when mid=18c) due to sweep orders. These are lucky, not repeatable. The resulting spread collapse triggers PAUSE_60S, which is correct behavior.

- **Scanner snapshot limitation**: Scanner sees spread at scan time only. A market with 3c spread at 8AM may have 30c spread by game time. This is known and accepted — rank-based scoring mitigates it.

## Key Lessons Learned

Bugs discovered and fixed (do not repeat):

1. Bot running old code after edits (3x) → MANDATORY restart protocol
2. UUID string comparison for trade ordering → use `created_time` watermark
3. `orderbook` vs `orderbook_fp` API format → always use `_dollars` fields
4. `CANCEL_ALL` killing all markets → explicit per-action handling
5. Silent market deactivation → `deactivation_reason` field, 8 paths fixed
6. Discord notification spam → filter to FILL + critical risk events only
7. `FULL_STOP` on profitable session → added `current < 0` guard on drawdown gate
8. Skew pushing round-trip to 0 gross → profitability floor in `skewed_quotes()`
9. Scanner selecting net_spread=0 markets → net spread filter with <=8c cap
10. Scanner uses bare `python` in nohup launch → cron has no python in PATH → use `sys.executable` for subprocess launches

Cross-tick negative round-trips (YES+NO > 100c) are STOP-LOSSES, not bugs. Do NOT try to prevent them with cost-basis tracking.

## Kalshi API Notes

- **Auth**: RSA-PSS signatures (not simple API key headers)
- **Prices**: Dollar strings (`"0.7200"`), not cents
- **Orderbook**: `orderbook_fp` with `_dollars` arrays, not `orderbook` with cents
- **Hierarchy**: Series → Event → Market
- **Fees**: Maker `ceil(0.0175 × P × (1-P) × 100)`, taker 4x maker
- **Rate limit**: 20 reads/sec (Basic tier)
- **Resolution**: `result` = "yes"/"no"/"scalar"/empty; `settlement_value_dollars`
- **SDK**: `kalshi_python_sync` has bugs — use raw HTTP requests via `src/kalshi_client.py`

## Git Conventions

- Never commit directly to main
- Create feature branch for each session: `feature/descriptive-name`
- Merge to main only after full test suite passes

## MANDATORY: Test-Driven Development (TDD)

For ALL new code in this project:

1. Write tests FIRST — including edge cases and real API data formats
2. Run tests — verify they FAIL (red)
3. Write implementation code
4. Run tests — verify they PASS (green)
5. Only then commit

Tests must include:
- Happy path
- Edge cases (empty data, null fields, boundary values)
- Real API response formats (not mocked/assumed schemas)
- Error recovery (what happens after a failure?)

NEVER write implementation before tests exist and fail.

## MANDATORY: Restart bot after code changes

After ANY change to `src/mm/*.py`, `scripts/paper_mm.py`, or `scripts/monitor_drain.py`:
1. Commit the changes
2. Kill running bot (`pkill -9 -f paper_mm`)
3. Restart bot
4. Verify new code is loaded in startup log

NEVER leave a bot running on old code.

## Compact conversation rules

Always keep the following information when compacting the current conversation/session:
- The current file direction now being edited
- The test failure information
- The infrastructure decision strategy made during the current session
