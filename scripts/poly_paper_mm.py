#!/usr/bin/env python3
"""
Paper trading market maker for Polymarket US.

Reuses the Kalshi MM engine (OBI microprice, continuous skew, dynamic spread,
4-layer risk) but with PolyClient adapter. Paper-only: no real orders placed.

Key differences from Kalshi paper_mm.py:
  - Uses PolyClient instead of KalshiClient
  - Slugs instead of tickers
  - Maker rebates (negative fees) instead of maker fees
  - No trades endpoint — fill simulation via orderbook snapshots
  - Separate DB: data/poly_mm_paper.db

Usage:
    python scripts/poly_paper_mm.py --slugs SLUG1,SLUG2 --duration 300
    python scripts/poly_paper_mm.py --slugs SLUG1 --duration 86400 --size 2
"""

import argparse
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.poly_client import PolyClient, calculate_maker_fee
from src.mm.state import MarketState, GlobalState
from src.mm.engine import MMEngine, discord_notify
from src.mm.db import MMDatabase


def main():
    parser = argparse.ArgumentParser(
        description="Paper trading market maker — Polymarket US")
    parser.add_argument("--slugs", required=True,
                        help="Comma-separated market slugs")
    parser.add_argument("--duration", type=int, default=86400,
                        help="Seconds to run (default: 24h)")
    parser.add_argument("--size", type=int, default=2,
                        help="Contracts per order (default: 2)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Seconds between ticks per market (default: 10)")
    parser.add_argument("--db-path", default="data/poly_mm_paper.db")
    args = parser.parse_args()

    # Auth is optional for paper trading (we only read orderbooks)
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret_key = os.getenv("POLYMARKET_SECRET_KEY")

    if key_id and secret_key:
        client = PolyClient(key_id=key_id, secret_key=secret_key)
        print("  Auth: configured (read + write)")
    else:
        client = PolyClient()
        print("  Auth: public only (read-only)")

    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + \
                 uuid.uuid4().hex[:6]

    db = MMDatabase(args.db_path, session_id)
    gs = GlobalState(session_id=session_id)

    # Initialize markets — slug is used as ticker throughout the engine
    for slug in slugs:
        gs.markets[slug] = MarketState(ticker=slug)

    engine = MMEngine(client, db, gs, order_size=args.size)

    # Track rebates earned per market for session summary
    rebates_earned = {slug: 0.0 for slug in slugs}

    # Graceful shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        print("\nShutting down gracefully...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Header
    n = len(slugs)
    MM_VERSION = "v1: Polymarket US — OBI + skew + dynamic spread + maker rebates"
    print(f"\nPoly Paper MM | {MM_VERSION}")
    print(f"  {n} markets | {args.size} contracts | "
          f"{args.interval}s interval")
    print(f"  Session: {session_id}")
    print(f"  Started: {datetime.now(timezone.utc).isoformat()} | "
          f"Duration: {args.duration}s | DB: {args.db_path}")
    print("-" * 70)

    discord_notify(
        f"**Poly Paper MM Started** | {n} markets | session={session_id}\n"
        f"Slugs: {', '.join(slugs)}")

    active_slugs = list(slugs)
    start = time.time()
    cycle = 0
    last_summary_time = start
    SUMMARY_INTERVAL = 43200  # 12h

    try:
        while not shutdown and (time.time() - start) < args.duration:
            active_slugs = [s for s in slugs if gs.markets[s].active]
            if not active_slugs:
                print("All markets inactive. Stopping.")
                break

            sleep_time = args.interval / max(len(active_slugs), 1)

            for i, slug in enumerate(active_slugs):
                if shutdown:
                    break
                if cycle % len(active_slugs) != i:
                    continue

                ms = gs.markets[slug]
                try:
                    engine.tick_one_market(ms)

                    # Track rebates on fills
                    if ms.realized_pnl != 0 and ms.net_inventory != 0:
                        mid = ms.midpoint_history[-1][1] if ms.midpoint_history else 50
                        rebate = calculate_maker_fee(int(mid))
                        rebates_earned[slug] += abs(rebate)

                except Exception as e:
                    print(f"  UNEXPECTED ERROR on {slug}: {e}",
                          file=sys.stderr)
                    engine._cancel_orders(ms, f"unexpected_error: {e}")

            cycle += 1

            # Periodic summary
            now_ts = time.time()
            if now_ts - last_summary_time >= SUMMARY_INTERVAL:
                elapsed_h = (now_ts - start) / 3600
                active_count = len(active_slugs)
                total_rebates = sum(rebates_earned.values())
                summary = (
                    f"**Poly Paper MM 12h Summary** | {elapsed_h:.1f}h | "
                    f"{active_count}/{n} active | "
                    f"pnl={gs.total_pnl:.1f}c "
                    f"(+{total_rebates:.1f}c rebates) | "
                    f"session={session_id}")
                print(f"\n{'=' * 70}")
                print(f"12H SUMMARY ({elapsed_h:.1f}h)")
                print(f"  Active: {active_count}/{n} markets")
                print(f"  Total P&L: {gs.total_pnl:.1f}c | "
                      f"Rebates: +{total_rebates:.1f}c")
                for s, ms in gs.markets.items():
                    status = ("ACTIVE" if ms.active
                              else f"EXIT({ms.deactivation_reason})")
                    print(f"  {s}: inv={ms.net_inventory} "
                          f"pnl={ms.realized_pnl:.1f}c [{status}]")
                print(f"{'=' * 70}\n")
                discord_notify(summary)
                last_summary_time = now_ts

            time.sleep(sleep_time)

    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr)

    # Shutdown: cancel orders and write final snapshots
    for ms in gs.markets.values():
        engine._cancel_orders(ms, "shutdown")
        if ms.midpoint_history:
            mid = ms.midpoint_history[-1][1]
            best_yb = int(mid - 2)
            y_ask = int(mid + 2)
            engine._write_snapshot(ms, best_yb, y_ask,
                                   y_ask - best_yb, mid)

    # Session summary
    elapsed = time.time() - start
    total_rebates = sum(rebates_earned.values())
    gross_pnl = gs.total_pnl
    net_pnl = gross_pnl + total_rebates

    print(f"\n{'=' * 70}")
    print("SESSION SUMMARY — POLYMARKET US")
    print(f"{'=' * 70}")
    print(f"  Duration:           {elapsed/3600:.1f}h")
    print(f"  Session:            {session_id}")
    print(f"  Platform:           Polymarket US (maker rebates)")

    for slug, ms in gs.markets.items():
        rebate = rebates_earned.get(slug, 0)
        print(f"\n  {slug}:")
        print(f"    Realized P&L:     {ms.realized_pnl:.1f}c")
        print(f"    Unrealized P&L:   {ms.unrealized_pnl:.1f}c")
        print(f"    Maker rebates:    +{rebate:.1f}c")
        print(f"    Net inventory:    {ms.net_inventory}")
        print(f"    Active:           {ms.active}")
        if not ms.active:
            print(f"    Exit reason:      {ms.deactivation_reason}")

    print(f"\n  GLOBAL:")
    print(f"    Gross P&L:        {gross_pnl:.1f}c")
    print(f"    Maker rebates:    +{total_rebates:.1f}c")
    print(f"    Net P&L:          {net_pnl:.1f}c")
    print(f"    Peak P&L:         {gs.peak_total_pnl:.1f}c")
    print(f"    DB:               {args.db_path}")

    discord_notify(
        f"**Poly Paper MM Ended** | {elapsed/3600:.1f}h | "
        f"gross={gross_pnl:.1f}c rebates=+{total_rebates:.1f}c "
        f"net={net_pnl:.1f}c | session={session_id}")

    db.close()

    # Auto-generate session summary
    try:
        from scripts.session_summary import generate_summary
        summary = generate_summary(args.db_path, session_id)
        sessions_dir = Path(".claude/sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sessions_dir / f"poly-{session_id}.md"
        summary_path.write_text(summary)
        print(f"\nSession summary: {summary_path}")
    except Exception as e:
        print(f"  Warning: session summary failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
