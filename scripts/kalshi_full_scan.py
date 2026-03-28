#!/usr/bin/env python3
"""
Full Kalshi market scan — ALL categories, not just sports.

Diagnostic tool to find MM-viable markets across the entire exchange.
Outputs category summary + top 30 markets by net_spread.

Usage:
    python scripts/kalshi_full_scan.py
    python scripts/kalshi_full_scan.py --top 50 --max-check 100
"""

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, PROD_BASE
from scripts.kalshi_mm_scanner import _parse_book_levels


OUTPUT_DIR = Path("data/kalshi_diagnostic")


def net_spread_cents(spread: int, midpoint: float) -> int:
    p = midpoint / 100
    fee_per_side = math.ceil(0.0175 * p * (1 - p) * 100)
    return spread - 2 * fee_per_side


def fetch_all_events(client: KalshiClient) -> list[dict]:
    """Fetch ALL active events with nested markets."""
    all_events = []
    cursor = None

    for _ in range(200):  # safety cap
        data = client.get_events(limit=200, with_nested_markets=True,
                                 status="open", cursor=cursor)
        batch = data.get("events", [])
        all_events.extend(batch)
        cursor = data.get("cursor")
        time.sleep(0.06)  # ~16 req/s, under 20/s limit
        if not cursor or not batch:
            break

    return all_events


def extract_candidates(events: list[dict]) -> list[dict]:
    """Extract all tradeable markets from events."""
    now = datetime.now(timezone.utc)
    candidates = []

    for ev in events:
        category = ev.get("category", "Unknown")

        for m in ev.get("markets", []):
            ticker = m.get("ticker", "")
            exp = m.get("expected_expiration_time", "")
            if not exp:
                continue

            # Skip expired
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if exp_dt < now:
                    continue
                hours_to_exp = (exp_dt - now).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            # Parse prices
            yb_str = m.get("yes_bid_dollars", "0") or "0"
            ya_str = m.get("yes_ask_dollars", "0") or "0"
            yb = int(round(float(yb_str) * 100))
            ya = int(round(float(ya_str) * 100))

            if yb <= 0 or ya <= 0:
                continue

            spread = ya - yb
            midpoint = (yb + ya) / 2

            # Skip extreme prices (outside 20-80c)
            if midpoint < 20 or midpoint > 80:
                continue

            candidates.append({
                "ticker": ticker,
                "title": (m.get("title") or m.get("yes_sub_title") or "")[:70],
                "event": ev.get("title", "")[:50],
                "category": category,
                "spread": spread,
                "midpoint": midpoint,
                "net_spread": net_spread_cents(spread, midpoint),
                "hours_to_exp": round(hours_to_exp, 1),
                "volume_24h": int(float(m.get("volume_24h_fp", "0") or "0")),
            })

    return candidates


def deep_check_batch(client: KalshiClient, candidates: list[dict],
                     max_check: int = 200) -> list[dict]:
    """Fetch orderbooks + trades for top candidates."""
    now = datetime.now(timezone.utc)

    # Sort by net_spread descending to check best candidates first
    candidates.sort(key=lambda c: c["net_spread"], reverse=True)
    to_check = candidates[:max_check]

    print(f"  Deep-checking {len(to_check)} markets (orderbook + trades)...")

    for i, c in enumerate(to_check):
        if i > 0 and i % 50 == 0:
            print(f"    ... {i}/{len(to_check)}")

        try:
            data = client.get_orderbook(c["ticker"], depth=10)
            yes_levels, no_levels = _parse_book_levels(data)

            yes_depth = sum(s for _, s in yes_levels) if yes_levels else 0
            no_depth = sum(s for _, s in no_levels) if no_levels else 0
            yes_best = yes_levels[0][1] if yes_levels else 0
            no_best = no_levels[0][1] if no_levels else 0

            c["yes_depth"] = yes_depth
            c["no_depth"] = no_depth
            c["max_best_depth"] = max(yes_best, no_best)

            if yes_depth > 0 and no_depth > 0:
                c["symmetry"] = round(yes_depth / no_depth, 3)
            else:
                c["symmetry"] = 0.0

        except Exception:
            c["yes_depth"] = 0
            c["no_depth"] = 0
            c["max_best_depth"] = 0
            c["symmetry"] = 0.0

        # Trades (every 3rd market to stay under rate limit)
        if i % 3 == 0:
            try:
                trade_data = client.get_trades(c["ticker"], limit=100)
                trades = trade_data.get("trades", [])
                cutoff_1h = now - timedelta(hours=1)
                recent = []
                for t in trades:
                    try:
                        ts = datetime.fromisoformat(
                            t["created_time"].replace("Z", "+00:00"))
                        if ts >= cutoff_1h:
                            recent.append(ts)
                    except (KeyError, ValueError):
                        continue
                if len(recent) >= 2:
                    span_h = max(
                        (max(recent) - min(recent)).total_seconds() / 3600,
                        0.01)
                    c["trades_per_hour"] = round(len(recent) / span_h, 1)
                else:
                    c["trades_per_hour"] = float(len(recent))
            except Exception:
                c["trades_per_hour"] = 0.0
        else:
            c["trades_per_hour"] = -1  # not checked

        time.sleep(0.06)  # rate limit

    return to_check


def build_category_summary(candidates: list[dict]) -> list[dict]:
    """Group by category and compute aggregate metrics."""
    by_cat = defaultdict(list)
    for c in candidates:
        by_cat[c["category"]].append(c)

    summary = []
    for cat, markets in sorted(by_cat.items()):
        spreads = [m["spread"] for m in markets]
        nets = [m["net_spread"] for m in markets]
        tphs = [m["trades_per_hour"] for m in markets
                if m.get("trades_per_hour", -1) >= 0]
        viable = sum(1 for n in nets if n >= 1)

        summary.append({
            "category": cat,
            "markets": len(markets),
            "avg_spread": round(sum(spreads) / len(spreads), 1),
            "avg_net_spread": round(sum(nets) / len(nets), 1),
            "pct_viable": round(100 * viable / len(markets), 1),
            "avg_trades_hr": round(sum(tphs) / max(len(tphs), 1), 1),
            "max_net_spread": max(nets),
        })

    summary.sort(key=lambda s: s["pct_viable"], reverse=True)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Full Kalshi market scan — all categories")
    parser.add_argument("--top", type=int, default=30,
                        help="Number of top markets to show (default: 30)")
    parser.add_argument("--max-check", type=int, default=200,
                        help="Max markets to deep-check (default: 200)")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    client = KalshiClient(api_key, pk_path, PROD_BASE)

    print("=" * 70)
    print("Kalshi Full Market Scan — All Categories")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Phase 1: Fetch all events
    print("\n  Phase 1: Fetching all active events...")
    events = fetch_all_events(client)
    print(f"  Found {len(events)} active events")

    # Phase 2: Extract candidates (midpoint 20-80, not expired)
    candidates = extract_candidates(events)
    print(f"  {len(candidates)} markets with midpoint 20-80c")

    if not candidates:
        print("\n  No viable markets found.")
        return

    # Phase 3: Deep check top candidates
    print(f"\n  Phase 2: Deep-checking top {args.max_check} by net_spread...")
    checked = deep_check_batch(client, candidates, max_check=args.max_check)

    # Report A: Category summary
    print("\n" + "=" * 70)
    print("REPORT A: Category Summary")
    print("=" * 70)

    cat_summary = build_category_summary(candidates)
    header = (f"{'Category':<20} {'Mkts':>5} {'Avg Sprd':>8} {'Avg Net':>7} "
              f"{'% Net>=1':>8} {'Avg Trd/h':>9} {'Max Net':>7}")
    print(header)
    print("-" * len(header))
    for s in cat_summary:
        print(f"{s['category']:<20} {s['markets']:5d} {s['avg_spread']:8.1f} "
              f"{s['avg_net_spread']:7.1f} {s['pct_viable']:7.1f}% "
              f"{s['avg_trades_hr']:9.1f} {s['max_net_spread']:7d}")

    # Report B: Top N markets by net_spread
    print(f"\n{'=' * 70}")
    print(f"REPORT B: Top {args.top} Markets by Net Spread")
    print("=" * 70)

    top = sorted(checked, key=lambda c: c["net_spread"], reverse=True)[:args.top]
    header2 = (f"{'#':>2} {'Category':<15} {'Ticker':<35} {'Sprd':>4} "
               f"{'Net':>4} {'Mid':>4} {'L1Q':>7} {'Trd/h':>6} "
               f"{'Sym':>5} {'Exp_h':>5}")
    print(header2)
    print("-" * len(header2))
    for i, c in enumerate(top, 1):
        sym_s = f"{c.get('symmetry', 0):.2f}"
        tph = c.get("trades_per_hour", -1)
        tph_s = f"{tph:.0f}" if tph >= 0 else "?"
        l1q = c.get("max_best_depth", 0)
        print(f"{i:2d} {c['category']:<15} {c['ticker']:<35} "
              f"{c['spread']:4d} {c['net_spread']:4d} {c['midpoint']:4.0f} "
              f"{l1q:7d} {tph_s:>6} {sym_s:>5} {c['hours_to_exp']:5.1f}")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_file = OUTPUT_DIR / f"full_scan_{date_str}.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_events": len(events),
            "total_candidates": len(candidates),
            "category_summary": cat_summary,
            "top_markets": top,
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
