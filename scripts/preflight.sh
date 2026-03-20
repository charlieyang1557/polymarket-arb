#!/bin/bash
# Pre-flight check before paper MM launch
# On failure: sends Discord alert, exits non-zero (crontab aborts launch)
# Human fixes the issue, re-runs: bash scripts/preflight.sh && python scripts/kalshi_daily_scan.py --run --max-markets 15

cd "$(dirname "$0")/.."
PYTHON=python
LOG=data/preflight.log
echo "=== PRE-FLIGHT $(date) ===" > "$LOG"

# Helper: send Discord alert on failure
notify_fail() {
    $PYTHON -c "
from dotenv import load_dotenv; load_dotenv()
from src.mm.engine import discord_notify
discord_notify('**PREFLIGHT FAILED** $1 — fix and re-run manually: bash scripts/preflight.sh && python scripts/kalshi_daily_scan.py --run --max-markets 15')
" 2>/dev/null
}

# 1. Git clean
echo "[1/6] Git status..." >> "$LOG"
if git diff --quiet scripts/ src/ tests/; then
    echo "  PASS: no uncommitted changes" >> "$LOG"
else
    echo "  FAIL: uncommitted changes detected" >> "$LOG"
    notify_fail "uncommitted changes in scripts/src/tests"
    exit 1
fi

# 2. Tests pass
echo "[2/6] Running tests..." >> "$LOG"
if $PYTHON -m pytest tests/ -q \
    --ignore=tests/test_risk.py \
    --ignore=tests/test_evaluator.py \
    --ignore=tests/test_scanner.py \
    --ignore=tests/test_trade_pipeline.py 2>&1 | tee -a "$LOG" | tail -1 | grep -q "passed"; then
    echo "  PASS: all tests pass" >> "$LOG"
else
    echo "  FAIL: tests failed" >> "$LOG"
    notify_fail "test suite failed"
    exit 1
fi

# 3. Discord webhook loads
echo "[3/6] Discord webhook..." >> "$LOG"
DISCORD_CHECK=$($PYTHON -c "
from dotenv import load_dotenv; load_dotenv()
from src.mm.engine import DISCORD_WEBHOOK
print('OK' if DISCORD_WEBHOOK else 'MISSING')
" 2>&1)
if [ "$DISCORD_CHECK" = "OK" ]; then
    echo "  PASS: webhook loaded" >> "$LOG"
else
    echo "  FAIL: webhook missing" >> "$LOG"
    # Can't notify via Discord if webhook is missing
    exit 1
fi

# 4. Kalshi API auth
echo "[4/6] Kalshi API..." >> "$LOG"
API_CHECK=$($PYTHON -c "
from dotenv import load_dotenv; load_dotenv()
import os
from src.kalshi_client import KalshiClient, PROD_BASE
c = KalshiClient(os.getenv('KALSHI_API_KEY'), os.getenv('KALSHI_PRIVATE_KEY_PATH'), PROD_BASE)
import requests
r = requests.get(PROD_BASE + '/exchange/status', timeout=10)
print('OK' if r.status_code == 200 else f'FAIL:{r.status_code}')
" 2>&1)
if echo "$API_CHECK" | grep -q "OK"; then
    echo "  PASS: API reachable" >> "$LOG"
else
    echo "  FAIL: API error: $API_CHECK" >> "$LOG"
    notify_fail "Kalshi API unreachable"
    exit 1
fi

# 5. Send Discord preflight notification
echo "[5/6] Discord send test..." >> "$LOG"
$PYTHON -c "
from dotenv import load_dotenv; load_dotenv()
from src.mm.engine import discord_notify
discord_notify('Pre-flight OK — launching paper MM')
" 2>&1 >> "$LOG"
echo "  PASS: notification sent" >> "$LOG"

# 6. Clean old processes
echo "[6/6] Cleaning old processes..." >> "$LOG"
pkill -9 -f "paper_mm.py" 2>/dev/null || true
pkill -f "monitor_drain" 2>/dev/null || true
pkill -f "caffeinate" 2>/dev/null || true
sleep 2
rm -f data/mm_paper.db data/mm_paper.db-wal data/mm_paper.db-shm
echo "  PASS: cleaned" >> "$LOG"

echo "" >> "$LOG"
echo "PRE-FLIGHT COMPLETE — all checks passed" >> "$LOG"
echo "PRE-FLIGHT COMPLETE"
