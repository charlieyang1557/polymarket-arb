"""
One-shot scan script for debugging and testing.

Usage:
    python scripts/scan_once.py

Fetches all active events, runs both scanners, and prints results.
Does NOT place any trades.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.client import PolymarketClient
from src.scanner.rebalance import RebalanceScanner
from src.scanner.logical import LogicalScanner

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main():
    client = PolymarketClient()
    events = client.get_all_active_events()
    print(f"Fetched {len(events)} active events")

    scanners = [RebalanceScanner(client), LogicalScanner(client)]
    all_opps = []
    for scanner in scanners:
        opps = scanner.scan(events)
        all_opps.extend(opps)

    print(f"\nFound {len(all_opps)} opportunities:")
    for opp in all_opps:
        print(f"  [{opp.type}] {opp.event_ids[0][:30]}... "
              f"edge={opp.expected_profit_pct:.2f}% "
              f"confidence={opp.confidence:.1f}")


if __name__ == "__main__":
    main()
