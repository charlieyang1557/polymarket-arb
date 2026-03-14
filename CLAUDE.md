# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated arbitrage scanner/trader for Polymarket prediction markets. Detects two opportunity types:
- **Type 1 (Rebalance)**: Neg-risk events where sum(YES ask prices) < $1.00 — guaranteed profit
- **Type 2 (Logical)**: Logically related markets with inconsistent prices (e.g., "by June" vs "by December")

Progressive phases: scanner (Phase 1) → paper trading (Phase 2) → live trading (Phase 3+).

## Commands

```bash
pip install -r requirements.txt        # Install dependencies
python scripts/scan_once.py            # One-shot scan (no trades)
python main.py                         # Paper trading loop (30s interval)
python main.py --live                  # Live trading (Phase 3+, needs .env keys)
pytest tests/ -v                       # Run tests (currently skeleton stubs)
```

## Architecture

```
main.py (async loop every 30s)
  ├── src/client.py          → Polymarket API wrapper (Gamma + CLOB APIs)
  ├── src/scanner/rebalance.py → Type 1 detection
  ├── src/scanner/logical.py   → Type 2 detection (rule-based via config/relationships.py)
  ├── src/evaluator.py       → Fee/liquidity/edge filtering
  ├── src/risk.py            → Position limits, stop-loss, circuit breakers
  ├── src/trader/paper.py    → Simulated execution
  ├── src/trader/live.py     → Real execution via py-clob-client (Phase 3+)
  ├── src/db.py              → SQLite persistence (sqlite-utils)
  └── dashboard/terminal.py  → Rich terminal UI
```

Data flows: Client fetches events → Scanners emit `ArbitrageOpportunity` → Evaluator filters → RiskManager gates → Trader executes → DB persists.

## Key Models (src/models.py)

All Pydantic v2. Key types: `Event` → `Market` → `Outcome`, `ArbitrageOpportunity`, `Trade`, `RiskStatus`.

## Configuration

- **Risk parameters**: `config/settings.py` — trade limits, min edge (1% Type 1, 3% Type 2), loss limits, circuit breakers
- **Type 2 rules**: `config/relationships.py` — manual relationship definitions (temporal, threshold, sports, macro)
- **Environment**: `.env` from `.env.example` — only needed for Phase 3+ live trading (Ed25519 private key, Polygon wallet address)

## Current State / Known Issues

Many components are **NotImplemented stubs**: evaluator, risk manager, traders, dashboard, tests. The scanners (`rebalance.py`, `logical.py`) have working logic but reference a non-existent `ArbitrageType` enum and construct `ArbitrageOpportunity` with field names that don't match the model definition (e.g., `opp_type` vs `type`, `net_profit` vs `expected_profit`). Also `rebalance.py:75` sets `best_ask`/`best_bid` attributes not defined on `Outcome`.

## Polymarket Domain Notes

- **Neg-risk markets**: mutually exclusive outcomes where all YES shares sum to $1
- Fee: 0.01% per trade (maker limit orders = 0 fee)
- No shorting — can only buy YES or NO tokens
- Settlement on market resolution (days to months, not intraday)
- APIs: Gamma (metadata) at gamma-api.polymarket.com, CLOB (prices/orders) at clob.polymarket.com
- Rate limit: 60 req/min with exponential backoff retry in client

## Git Conventions
- Never commit directly to main
- Create feature branch for each session: feature/descriptive-name
- Merge to main only after scan_once.py or relevant tests pass
- PR description: paste the superpowers plan summary

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

After ANY change to `src/mm/*.py` or `scripts/paper_mm.py`:
1. Commit the changes
2. Kill running bot (`pkill -f paper_mm`)
3. Restart bot
4. Verify new code is loaded in startup log

NEVER leave a bot running on old code.

## Compact conversation rules

Always keep the following information when compacting the current conversation/session:
- The current file direction now being edited
- The test failure information
- The infrastructure decision strategy made during the current session