"""
Export trade log to CSV.

Usage:
    python scripts/export_trades.py --output trades.csv
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Export trade history to CSV")
    parser.add_argument("--output", default="trades.csv", help="Output CSV file path")
    args = parser.parse_args()
    raise NotImplementedError("Implement after Phase 2")


if __name__ == "__main__":
    main()
