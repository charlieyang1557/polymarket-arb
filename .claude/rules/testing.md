# Testing Rules — MANDATORY

## TDD Workflow
- Write failing tests BEFORE implementation — no exceptions
- Run relevant test suite after every .py file edit
- Never commit code with failing tests
- MM test command: python -m pytest tests/test_mm_*.py tests/test_*skew*.py tests/test_*spread*.py tests/test_*obi*.py tests/test_pregame*.py tests/test_silent*.py tests/test_monitor.py tests/test_inventory*.py -q

## Integration Tests
- Use real API response fixtures, not assumed schemas
- Kalshi API returns `orderbook_fp` with `_dollars` fields — always test with this format
- Test edge cases: empty orderbook, crossed book, single-side book

## Before Any Bot Restart
- Run full MM test suite (command above)
- All tests must pass before restarting
- Log test count in commit message
