#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p data

echo "[$(date)] Starting preflight..."
bash scripts/preflight.sh > data/preflight.log 2>&1 || true
echo "[$(date)] Preflight done"

echo "[$(date)] Starting scanner (--smart-run)..."
python scripts/kalshi_daily_scan.py --smart-run --max-markets 15
echo "[$(date)] Scanner done."
