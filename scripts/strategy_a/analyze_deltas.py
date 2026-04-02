#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze accumulated odds comparison data.

Reads odds_comparison.db and prints summary statistics on
Pinnacle vs Polymarket deltas.

Usage:
    python3 scripts/strategy_a/analyze_deltas.py
    python3 scripts/strategy_a/analyze_deltas.py --db path/to/db
"""

import argparse
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from src.strategy_a.odds_db import OddsDB

# Polymarket taker fee
TAKER_FEE_COEFF = 0.0175
SLIPPAGE_CENTS = 1

DB_PATH = "data/strategy_a/odds_comparison.db"


def taker_fee_cents(price_cents: int) -> float:
    p = price_cents / 100
    return math.ceil(TAKER_FEE_COEFF * p * (1 - p) * 100)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Pinnacle vs Polymarket delta data")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"No data yet: {args.db}")
        print("Run odds_collector.py first to collect snapshots.")
        sys.exit(0)

    db = OddsDB(args.db)
    rows = db.get_all()
    db.close()

    if not rows:
        print("No snapshots in database.")
        sys.exit(0)

    print(f"{'='*60}")
    print(f"ODDS DELTA ANALYSIS")
    print(f"{'='*60}")
    print(f"  Total snapshots: {len(rows)}")

    # Unique events (by slug)
    unique_slugs = set(r["slug"] for r in rows)
    print(f"  Unique markets:  {len(unique_slugs)}")

    # Date range
    timestamps = sorted(set(r["timestamp"] for r in rows))
    print(f"  Date range:      {timestamps[0][:10]} to {timestamps[-1][:10]}")
    print(f"  Snapshot times:  {len(timestamps)}")

    # --- Delta distribution ---
    deltas = [abs(r["delta_home"]) for r in rows]
    avg_delta = sum(deltas) / len(deltas)
    median_delta = sorted(deltas)[len(deltas) // 2]

    print(f"\n  Delta Distribution (|Pinnacle - Polymarket|):")
    print(f"    Mean:   {avg_delta:.1%}")
    print(f"    Median: {median_delta:.1%}")

    thresholds = [0.01, 0.02, 0.03, 0.05, 0.07, 0.10]
    print(f"\n    {'Threshold':>10} {'Count':>6} {'Pct':>6}")
    print(f"    {'-'*25}")
    for t in thresholds:
        count = sum(1 for d in deltas if d > t)
        print(f"    {t:10.0%} {count:6d} {100*count/len(deltas):5.1f}%")

    # --- By sport ---
    print(f"\n  By Sport:")
    sport_groups = defaultdict(list)
    for r in rows:
        sport_groups[r["sport"]].append(r)

    print(f"    {'Sport':<25} {'N':>5} {'Avg|Δ|':>8} {'|Δ|>3%':>7}")
    print(f"    {'-'*48}")
    for sport, sport_rows in sorted(sport_groups.items(),
                                     key=lambda x: -len(x[1])):
        d = [abs(r["delta_home"]) for r in sport_rows]
        avg = sum(d) / len(d)
        big = sum(1 for x in d if x > 0.03)
        print(f"    {sport:<25} {len(sport_rows):5d} {avg:8.1%} "
              f"{big:4d} ({100*big/len(d):.0f}%)")

    # --- By hours_to_game ---
    print(f"\n  By Hours to Game:")
    htg_buckets = [
        (0, 2, "0-2h"),
        (2, 6, "2-6h"),
        (6, 12, "6-12h"),
        (12, 24, "12-24h"),
        (24, 999, "24h+"),
    ]
    print(f"    {'Window':<10} {'N':>5} {'Avg|Δ|':>8}")
    print(f"    {'-'*26}")
    for lo, hi, label in htg_buckets:
        bucket = [r for r in rows if lo <= (r["hours_to_game"] or 0) < hi]
        if bucket:
            d = [abs(r["delta_home"]) for r in bucket]
            avg = sum(d) / len(d)
            print(f"    {label:<10} {len(bucket):5d} {avg:8.1%}")

    # --- Delta direction (bias check) ---
    print(f"\n  Delta Direction (is Polymarket consistently off?):")
    home_deltas = [r["delta_home"] for r in rows]
    pos = sum(1 for d in home_deltas if d > 0)
    neg = sum(1 for d in home_deltas if d < 0)
    avg_signed = sum(home_deltas) / len(home_deltas)
    print(f"    Pinnacle > Polymarket (home): {pos} ({100*pos/len(rows):.1f}%)")
    print(f"    Pinnacle < Polymarket (home): {neg} ({100*neg/len(rows):.1f}%)")
    print(f"    Mean signed delta: {avg_signed:+.3f}")
    if abs(avg_signed) > 0.02:
        direction = "Polymarket underprices home" if avg_signed > 0 else "Polymarket overprices home"
        print(f"    ** Systematic bias: {direction}")

    # --- Simulated PnL ---
    print(f"\n  Simulated PnL (if we bet on |delta| > threshold):")
    print(f"    Fee: ceil(0.0175 * p * (1-p) * 100) per trade")
    print(f"    Slippage: {SLIPPAGE_CENTS}c")
    print(f"    NOTE: No outcome data yet — this estimates gross potential")
    print(f"\n    {'Thresh':>7} {'Trades':>7} {'Avg|Δ|':>8} "
          f"{'MaxEdge/Trade':>13}")
    print(f"    {'-'*38}")

    for t in [0.02, 0.03, 0.05, 0.07]:
        qualifying = [r for r in rows if abs(r["delta_home"]) > t]
        if qualifying:
            d = [abs(r["delta_home"]) for r in qualifying]
            avg_d = sum(d) / len(d)
            # Max edge per trade in cents (before outcome)
            max_edge = avg_d * 100 - 2 - SLIPPAGE_CENTS  # ~2c avg fee
            print(f"    {t:7.0%} {len(qualifying):7d} {avg_d:8.1%} "
                  f"{max_edge:10.1f}c")

    print(f"\n  NEXT: Once markets settle, re-run with outcome data")
    print(f"  to compute actual PnL and Brier Score comparison.")


if __name__ == "__main__":
    main()
