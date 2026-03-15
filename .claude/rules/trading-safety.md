# Trading Safety Rules — MANDATORY

## Bot Restart Protocol (CRITICAL — violated 3+ times already)
After ANY change to `src/mm/*.py`, `scripts/paper_mm.py`, or `scripts/monitor_drain.py`:
1. `git add && git commit` the changes
2. `pkill -9 -f paper_mm` to kill running bot
3. Restart bot with same tickers
4. Verify startup log shows correct version string
5. NEVER leave a bot running on old code

## Order Limits
- Paper trading: max 5 contracts per order, max 10 single-side inventory
- Live trading: requires explicit `--live` flag (not yet implemented)
- Never place real orders without explicit human approval
- Daily loss limit: $20 paper, $5 live — hardcoded, cannot be overridden

## Risk Parameters
- Never modify risk limits (max_inventory, loss_limits, circuit breakers) without human approval
- Never disable or bypass L1-L4 risk checks
- GAMMA, MIN_SPREAD, MAX_INVENTORY changes require explicit discussion first
