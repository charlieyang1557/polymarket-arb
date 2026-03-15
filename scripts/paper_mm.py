#!/usr/bin/env python3
"""
Paper trading market maker for Kalshi.

Usage:
    python scripts/paper_mm.py                            # all Tier 1, 48h
    python scripts/paper_mm.py --tickers KXGREENLAND-29   # single market
    python scripts/paper_mm.py --duration 300             # 5 min test
    python scripts/paper_mm.py --size 3 --interval 15     # custom params

Standard startup (bot + watchdog):
    rm -f data/mm_paper.db data/mm_paper.db-wal data/mm_paper.db-shm
    nohup python -u scripts/paper_mm.py > data/mm_paper_run.log 2>&1 &
    nohup python -u scripts/monitor_drain.py > data/mm_monitor.log 2>&1 &
"""

import argparse
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, PROD_BASE
from src.mm.state import MarketState, GlobalState
from src.mm.engine import MMEngine, discord_notify
from src.mm.db import MMDatabase

DEFAULT_TICKERS = [
    "KXNCAAMBSPREAD-26MAR14UVMUMBC-UMBC2",
    "KXLALIGATOTAL-26MAR14GIRATH-2",
    "KXNBASPREAD-26MAR14BKNPHI-PHI7",
    "KXNBATOTAL-26MAR14BKNPHI-201",
    "KXNCAAMBSPREAD-26MAR14PENNHARV-HARV2",
    "KXNCAAMBSPREAD-26MAR14UVADUKE-DUKE8",
    "KXNCAAMBTOTAL-26MAR14DAYSLU-144",
]


def main():
    parser = argparse.ArgumentParser(description="Paper trading market maker")
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS),
                        help="Comma-separated market tickers")
    parser.add_argument("--duration", type=int, default=172800,
                        help="Seconds to run (default: 48h)")
    parser.add_argument("--size", type=int, default=2,
                        help="Contracts per order (default: 2)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Seconds between ticks per market (default: 10)")
    parser.add_argument("--db-path", default="data/mm_paper.db")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    tickers = [t.strip() for t in args.tickers.split(",")]
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + \
                 uuid.uuid4().hex[:6]

    client = KalshiClient(api_key, pk_path, PROD_BASE)
    db = MMDatabase(args.db_path, session_id)
    gs = GlobalState(session_id=session_id)

    for ticker in tickers:
        gs.markets[ticker] = MarketState(ticker=ticker)

    engine = MMEngine(client, db, gs, order_size=args.size)

    # Graceful shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        print("\nShutting down gracefully...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Header
    n = len(tickers)
    MM_VERSION = "v2: OBI + continuous skew + dynamic spread"
    print(f"Paper MM | {MM_VERSION}")
    print(f"  {n} markets | {args.size} contracts | "
          f"{args.interval}s interval")
    print(f"Session: {session_id}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()} | "
          f"Duration: {args.duration}s | DB: {args.db_path}")
    print("-" * 70)

    discord_notify(
        f"**Paper MM Started** | {n} markets | session={session_id}")

    active_tickers = list(tickers)
    sleep_time = args.interval / max(len(active_tickers), 1)
    start = time.time()
    cycle = 0

    try:
        while not shutdown and (time.time() - start) < args.duration:
            active_tickers = [t for t in tickers
                              if gs.markets[t].active]
            if not active_tickers:
                print("All markets inactive. Stopping.")
                break

            sleep_time = args.interval / max(len(active_tickers), 1)

            for i, ticker in enumerate(active_tickers):
                if shutdown:
                    break
                # Stagger: only tick this market on its turn
                if cycle % len(active_tickers) != i:
                    continue
                ms = gs.markets[ticker]
                try:
                    engine.tick_one_market(ms)
                except Exception as e:
                    print(f"  UNEXPECTED ERROR on {ticker}: {e}",
                          file=sys.stderr)
                    # Per spec: unexpected error -> cancel all orders
                    engine._cancel_orders(ms, f"unexpected_error: {e}")

            cycle += 1
            time.sleep(sleep_time)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr)

    # Shutdown: cancel orders and write final snapshots
    for ms in gs.markets.values():
        engine._cancel_orders(ms, "shutdown")
        # Write final snapshot for each market
        if ms.midpoint_history:
            mid = ms.midpoint_history[-1][1]
            best_yb = int(mid - 2)  # approximate from last midpoint
            y_ask = int(mid + 2)
            engine._write_snapshot(ms, best_yb, y_ask,
                                   y_ask - best_yb, mid)

    # Summary
    elapsed = time.time() - start
    print(f"\n{'=' * 70}")
    print("SESSION SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Duration:           {elapsed/3600:.1f}h")
    print(f"  Session:            {session_id}")
    for ticker, ms in gs.markets.items():
        print(f"\n  {ticker}:")
        print(f"    Realized P&L:     {ms.realized_pnl:.1f}c")
        print(f"    Unrealized P&L:   {ms.unrealized_pnl:.1f}c")
        print(f"    Total fees:       {ms.total_fees:.1f}c")
        print(f"    Net inventory:    {ms.net_inventory}")
        print(f"    Active:           {ms.active}")

    print(f"\n  GLOBAL:")
    print(f"    Total realized:   {gs.total_realized_pnl:.1f}c")
    print(f"    Total unrealized: {gs.total_unrealized_pnl:.1f}c")
    print(f"    Total P&L:        {gs.total_pnl:.1f}c")
    print(f"    Peak P&L:         {gs.peak_total_pnl:.1f}c")
    print(f"    DB:               {args.db_path}")

    discord_notify(
        f"**Paper MM Ended** | {elapsed/3600:.1f}h | "
        f"pnl={gs.total_pnl:.1f}c | session={session_id}")

    db.close()


if __name__ == "__main__":
    main()
