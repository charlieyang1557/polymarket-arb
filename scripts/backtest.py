"""
Backtester

Replays historical opportunity data from the DB to estimate
what returns would have been under different parameter settings.

Usage:
    python scripts/backtest.py --since 2024-01-01 --min-edge 1.5

See blueprint Phase 1 analysis goals for context.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Backtest arbitrage strategies")
    parser.add_argument("--since", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--min-edge", type=float, default=1.0, help="Min edge %% to simulate trading")
    args = parser.parse_args()
    raise NotImplementedError("Implement after Phase 1 data collection")


if __name__ == "__main__":
    main()
