#!/usr/bin/env python3
"""
Calibration data quality verification.

Checks whether the calibration signal is real or a data artifact:
1. Dumps raw fields from settled markets (what price fields exist?)
2. Measures time gap between last trade and settlement
3. Volume sanity check per bucket
4. Re-runs calibration excluding stale/thin markets

Usage:
    python scripts/kalshi_calibration_verify.py
    python scripts/kalshi_calibration_verify.py --max-events 2000 --sample-trades 100
"""

import argparse
import json
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
from scripts.kalshi_calibration import (
    fetch_settled_events, bucket_analysis, _binomial_pvalue,
)

OUTPUT_DIR = Path("data/kalshi_diagnostic")
BUCKET_SIZE = 10


def dump_settled_market_fields(client: KalshiClient, events: list[dict]):
    """Dump ALL fields from a few settled markets to discover API format."""
    print("\n" + "=" * 70)
    print("STEP 1: Raw field discovery — settled market API response")
    print("=" * 70)

    # Pick a few settled markets from different categories
    seen_cats = set()
    samples = []
    for ev in events:
        cat = ev.get("category", "Unknown")
        if cat in seen_cats:
            continue
        for m in ev.get("markets", []):
            if (m.get("result") or "").lower() in ("yes", "no"):
                samples.append((cat, m.get("ticker", "")))
                seen_cats.add(cat)
                break
        if len(samples) >= 3:
            break

    for cat, ticker in samples:
        print(f"\n--- {cat}: {ticker} ---")
        try:
            full = client.get_market(ticker)
            market = full.get("market", full)
            # Print all fields with values
            for k, v in sorted(market.items()):
                if v is not None and v != "" and v != "0" and v != 0:
                    val_str = str(v)[:80]
                    print(f"  {k:<35} = {val_str}")
            time.sleep(0.1)
        except Exception as e:
            print(f"  ERROR: {e}")


def extract_markets_with_timestamps(events: list[dict]) -> list[dict]:
    """Extract resolved markets preserving all timestamp fields."""
    markets = []

    for ev in events:
        category = ev.get("category", "Unknown")

        for m in ev.get("markets", []):
            ticker = m.get("ticker", "")
            result = (m.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue

            yes_won = result == "yes"

            # Try multiple price fields
            final_price = None
            price_source = None
            for field in ("last_price_dollars", "previous_yes_bid_dollars",
                          "yes_bid_dollars", "previous_price_dollars"):
                val = m.get(field)
                if val:
                    try:
                        p = float(val)
                        if 0 < p < 1:
                            final_price = int(round(p * 100))
                            price_source = field
                            break
                    except (ValueError, TypeError):
                        continue

            if final_price is None or final_price <= 0 or final_price >= 100:
                continue

            vol = int(float(m.get("volume_fp", "0") or "0"))

            # Capture all timestamp fields for staleness analysis
            markets.append({
                "ticker": ticker,
                "title": (m.get("title") or "")[:70],
                "category": category,
                "final_price": final_price,
                "price_source": price_source,
                "yes_won": yes_won,
                "volume": vol,
                "close_time": m.get("close_time") or m.get("expected_expiration_time", ""),
                "last_trade_time": None,  # filled in step 3
            })

    return markets


def measure_trade_staleness(client: KalshiClient, markets: list[dict],
                            sample_size: int = 100):
    """For a sample of markets, fetch last trade time and measure staleness."""
    print("\n" + "=" * 70)
    print("STEP 3: Trade staleness — time gap between last trade and settlement")
    print("=" * 70)

    # Sample from middle buckets (40-70c) where the signal is
    middle = [m for m in markets if 40 <= m["final_price"] <= 70]
    # Also sample from edge buckets for comparison
    edges = [m for m in markets if m["final_price"] <= 20 or m["final_price"] >= 80]

    import random
    random.seed(42)
    sample_mid = random.sample(middle, min(sample_size, len(middle)))
    sample_edge = random.sample(edges, min(sample_size // 2, len(edges)))

    def fetch_last_trade(market: dict) -> float | None:
        """Returns hours between last trade and close time, or None."""
        try:
            trade_data = client.get_trades(market["ticker"], limit=1)
            trades = trade_data.get("trades", [])
            if not trades:
                return None

            last_ts_str = trades[0].get("created_time", "")
            last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            market["last_trade_time"] = last_ts_str

            close_str = market.get("close_time", "")
            if not close_str:
                return None
            close_ts = datetime.fromisoformat(close_str.replace("Z", "+00:00"))

            gap_hours = (close_ts - last_ts).total_seconds() / 3600
            return gap_hours
        except Exception:
            return None

    print(f"\n  Fetching last trades for {len(sample_mid)} middle-bucket "
          f"(40-70c) markets...")
    mid_gaps = []
    for i, m in enumerate(sample_mid):
        gap = fetch_last_trade(m)
        if gap is not None:
            mid_gaps.append(gap)
        time.sleep(0.1)
        if (i + 1) % 20 == 0:
            print(f"    ... {i + 1}/{len(sample_mid)}")

    print(f"\n  Fetching last trades for {len(sample_edge)} edge-bucket "
          f"(0-20c, 80-100c) markets...")
    edge_gaps = []
    for i, m in enumerate(sample_edge):
        gap = fetch_last_trade(m)
        if gap is not None:
            edge_gaps.append(gap)
        time.sleep(0.1)

    # Report
    def gap_stats(gaps, label):
        if not gaps:
            print(f"  {label}: no data")
            return
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[len(gaps_sorted) // 2]
        avg = sum(gaps) / len(gaps)
        within_1h = sum(1 for g in gaps if g <= 1) / len(gaps) * 100
        within_24h = sum(1 for g in gaps if g <= 24) / len(gaps) * 100
        within_7d = sum(1 for g in gaps if g <= 168) / len(gaps) * 100
        print(f"  {label} (N={len(gaps)}):")
        print(f"    Median gap: {median:.1f}h | Mean: {avg:.1f}h")
        print(f"    Within  1h: {within_1h:.0f}%")
        print(f"    Within 24h: {within_24h:.0f}%")
        print(f"    Within  7d: {within_7d:.0f}%")

    gap_stats(mid_gaps, "Middle buckets (40-70c)")
    gap_stats(edge_gaps, "Edge buckets (0-20c, 80-100c)")

    # Return staleness data for downstream filtering
    return mid_gaps, edge_gaps


def volume_sanity_check(markets: list[dict]):
    """Volume distribution by price bucket."""
    print("\n" + "=" * 70)
    print("STEP 2: Volume sanity check by bucket")
    print("=" * 70)

    by_bucket = defaultdict(list)
    for m in markets:
        b = (m["final_price"] // BUCKET_SIZE) * BUCKET_SIZE
        by_bucket[b].append(m["volume"])

    header = f"{'Bucket':<10} {'Count':>6} {'Avg Vol':>8} {'Med Vol':>8} {'%<100':>6}"
    print(header)
    print("-" * len(header))

    for b in sorted(by_bucket.keys()):
        vols = sorted(by_bucket[b])
        n = len(vols)
        avg = sum(vols) / n
        med = vols[n // 2]
        thin = sum(1 for v in vols if v < 100) / n * 100
        print(f"{b}-{b + 10}c     {n:6d} {avg:8.0f} {med:8.0f} {thin:5.1f}%")


def filtered_calibration(markets: list[dict], min_volume: int = 100):
    """Re-run calibration excluding thin markets.

    Note: We can't filter by trade staleness here since we only
    fetched last_trade_time for a sample. But volume is available
    for all markets.
    """
    print("\n" + "=" * 70)
    print(f"STEP 4: Filtered calibration (volume >= {min_volume})")
    print("=" * 70)

    filtered = [m for m in markets if m["volume"] >= min_volume]
    print(f"  {len(filtered)}/{len(markets)} markets survive volume filter")

    if not filtered:
        print("  No markets survive filter.")
        return

    original = bucket_analysis(markets)
    clean = bucket_analysis(filtered)

    # Build lookup
    orig_by_bucket = {b["bucket"]: b for b in original}
    clean_by_bucket = {b["bucket"]: b for b in clean}

    header = (f"{'Bucket':<10} {'N_orig':>6} {'Edge_orig':>9} "
              f"{'N_filt':>6} {'Edge_filt':>9} {'P_filt':>8} {'Sig':>4}")
    print(header)
    print("-" * len(header))

    all_buckets = sorted(set(list(orig_by_bucket.keys()) +
                             list(clean_by_bucket.keys())))
    for bucket in all_buckets:
        o = orig_by_bucket.get(bucket, {"count": 0, "edge": 0})
        c = clean_by_bucket.get(bucket, {"count": 0, "edge": 0, "p_value": 1})
        sig = "***" if c.get("p_value", 1) < 0.05 else (
              "*" if c.get("p_value", 1) < 0.10 else "")
        print(f"{bucket:<10} {o['count']:6d} {o['edge']:+8.1f}% "
              f"{c['count']:6d} {c['edge']:+8.1f}% "
              f"{c.get('p_value', 1):8.4f} {sig:>4}")

    # Focus: 55-75c range comparison
    orig_focus = [m for m in markets if 55 <= m["final_price"] <= 75]
    filt_focus = [m for m in filtered if 55 <= m["final_price"] <= 75]

    if orig_focus and filt_focus:
        def summary(mlist, label):
            n = len(mlist)
            wins = sum(1 for m in mlist if m["yes_won"])
            wr = wins / n * 100
            exp = sum(m["final_price"] for m in mlist) / n
            print(f"  {label}: N={n}, win_rate={wr:.1f}%, "
                  f"expected={exp:.1f}%, edge={wr - exp:+.1f}%")

        print(f"\n  Focus: 55-75c range comparison")
        summary(orig_focus, "Original")
        summary(filt_focus, "Filtered")


def main():
    parser = argparse.ArgumentParser(
        description="Calibration data quality verification")
    parser.add_argument("--max-events", type=int, default=5000,
                        help="Max settled events to fetch (default: 5000)")
    parser.add_argument("--sample-trades", type=int, default=80,
                        help="Markets to sample for trade staleness (default: 80)")
    parser.add_argument("--min-volume", type=int, default=100,
                        help="Minimum volume for filtered analysis (default: 100)")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    client = KalshiClient(api_key, pk_path, PROD_BASE)

    print("=" * 70)
    print("KALSHI CALIBRATION — DATA QUALITY VERIFICATION")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Fetch settled events
    print(f"\n  Fetching settled events (max {args.max_events})...")
    events = fetch_settled_events(client, max_events=args.max_events)
    print(f"  Fetched {len(events)} settled events")

    # Step 1: Dump raw fields from a few settled markets
    dump_settled_market_fields(client, events)

    # Extract markets with timestamps
    markets = extract_markets_with_timestamps(events)
    print(f"\n  Extracted {len(markets)} resolved markets")

    if not markets:
        print("  No markets to analyze.")
        return

    # Step 2: Volume sanity check
    volume_sanity_check(markets)

    # Step 3: Trade staleness
    measure_trade_staleness(client, markets, sample_size=args.sample_trades)

    # Step 4: Filtered calibration
    filtered_calibration(markets, min_volume=args.min_volume)

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "calibration_verify.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_events": len(events),
            "total_markets": len(markets),
            "price_source_distribution": _price_source_dist(markets),
            "volume_by_bucket": _volume_by_bucket(markets),
            "original_calibration": bucket_analysis(markets),
            "filtered_calibration": bucket_analysis(
                [m for m in markets if m["volume"] >= args.min_volume]),
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


def _price_source_dist(markets):
    dist = defaultdict(int)
    for m in markets:
        dist[m.get("price_source", "unknown")] += 1
    return dict(dist)


def _volume_by_bucket(markets):
    by_bucket = defaultdict(list)
    for m in markets:
        b = (m["final_price"] // BUCKET_SIZE) * BUCKET_SIZE
        by_bucket[f"{b}-{b + 10}c"].append(m["volume"])
    return {k: {"count": len(v), "avg": round(sum(v) / len(v)),
                "median": sorted(v)[len(v) // 2]}
            for k, v in sorted(by_bucket.items())}


if __name__ == "__main__":
    main()
