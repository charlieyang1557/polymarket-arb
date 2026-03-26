#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Create data dir if needed
mkdir -p data

# Preflight
echo "[$(date)] Starting preflight..."
bash scripts/preflight.sh > data/preflight.log 2>&1 || true
echo "[$(date)] Preflight done"

# Scanner (finds markets, writes pending_markets.json for hot-add)
echo "[$(date)] Starting scanner..."
python scripts/kalshi_daily_scan.py --smart-run --max-markets 15
SCAN_EXIT=$?

if [ $SCAN_EXIT -ne 0 ]; then
    echo "[$(date)] WARNING: Scanner failed (exit $SCAN_EXIT)"
    exit 0  # Don't let PM2 restart loop
fi

# Check if paper_mm should start
TARGETS="data/kalshi_diagnostic/daily_targets.txt"
if [ ! -s "$TARGETS" ]; then
    echo "[$(date)] INFO: No markets passed filters; bot not started"
    exit 0
fi

# Launch paper_mm
TICKERS=$(cat "$TARGETS" | head -5 | tr '\n' ',' | sed 's/,$//')
echo "[$(date)] Launching paper_mm: $TICKERS"
python -u scripts/paper_mm.py --tickers "$TICKERS" --duration 86400 --size 2 --interval 10

echo "[$(date)] paper_mm.py exited, run.sh done."
