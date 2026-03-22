#!/bin/bash
set -e

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

source ~/miniconda3/etc/profile.d/conda.sh
conda activate base

cd ~/polymarket-arb
mkdir -p data/kalshi_diagnostic

echo "[$(date)] Python: $(which python)" >> data/preflight.log

echo "[$(date)] Starting preflight..." | tee -a data/preflight.log
bash scripts/preflight.sh >> data/preflight.log 2>&1
if [ $? -ne 0 ]; then
    echo "[$(date)] Preflight FAILED" >> data/preflight.log
    exit 1
fi

echo "[$(date)] Starting scanner..." | tee -a data/mm_paper_run.log
python scripts/kalshi_daily_scan.py --run --max-markets 15 \
    >> data/mm_paper_run.log 2>&1

sleep 5
PID=$(pgrep -f "paper_mm.py" || true)
if [ -n "$PID" ]; then
    caffeinate -i -s -w "$PID" &
    nohup python -u scripts/monitor_drain.py \
        >> data/mm_monitor.log 2>&1 &
    echo "[$(date)] Bot PID=$PID, caffeinate + monitor attached" \
        | tee -a data/mm_paper_run.log
else
    echo "[$(date)] WARNING: Bot did not start" \
        | tee -a data/mm_paper_run.log
    exit 1
fi

# Poll loop: paper_mm.py is launched by scanner via nohup,
# not a direct child of run.sh, so "wait" doesn't work.
while kill -0 "$PID" 2>/dev/null; do
    sleep 30
done
echo "[$(date)] paper_mm.py exited, run.sh done." \
    | tee -a data/mm_paper_run.log
