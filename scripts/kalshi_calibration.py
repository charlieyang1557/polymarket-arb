#!/usr/bin/env python3
"""
Kalshi Calibration Study — test for systematic mispricing.

Fetches all resolved markets, buckets by last YES price, and checks
whether actual win rates diverge from implied probability.

Usage:
    python scripts/kalshi_calibration.py
    python scripts/kalshi_calibration.py --max-events 500
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, PROD_BASE

OUTPUT_DIR = Path("data/kalshi_diagnostic")
BUCKET_SIZE = 10  # cents


def fetch_settled_events(client: KalshiClient,
                         max_events: int = 5000) -> list[dict]:
    """Paginate through all settled events with nested markets."""
    all_events = []
    cursor = None

    for _ in range(500):  # safety cap
        data = client.get_events(limit=200, with_nested_markets=True,
                                 status="settled", cursor=cursor)
        batch = data.get("events", [])
        all_events.extend(batch)
        cursor = data.get("cursor")
        time.sleep(0.1)

        if len(all_events) % 1000 < 200:
            print(f"    ... {len(all_events)} events fetched")

        if not cursor or not batch or len(all_events) >= max_events:
            break

    return all_events[:max_events]


def extract_resolved_markets(events: list[dict]) -> list[dict]:
    """Extract resolved markets with final price and outcome."""
    markets = []

    for ev in events:
        category = ev.get("category", "Unknown")

        for m in ev.get("markets", []):
            ticker = m.get("ticker", "")
            result = (m.get("result") or "").lower()

            # Only binary yes/no outcomes
            if result not in ("yes", "no"):
                continue

            yes_won = result == "yes"

            # Get final YES price — try multiple fields
            # Kalshi provides close/settlement info differently
            final_price = None

            # Option 1: yes_ask at close (last quoted price)
            # Option 2: settlement_value_dollars
            # Option 3: last_price_dollars
            for field in ("last_price_dollars", "previous_yes_bid_dollars",
                          "yes_bid_dollars"):
                val = m.get(field)
                if val:
                    try:
                        p = float(val)
                        if 0 < p < 1:
                            final_price = int(round(p * 100))
                            break
                    except (ValueError, TypeError):
                        continue

            # Fallback: previous_price
            if final_price is None:
                val = m.get("previous_price_dollars") or m.get("previous_yes_ask_dollars")
                if val:
                    try:
                        p = float(val)
                        if 0 < p < 1:
                            final_price = int(round(p * 100))
                    except (ValueError, TypeError):
                        pass

            if final_price is None or final_price <= 0 or final_price >= 100:
                continue

            vol = int(float(m.get("volume_fp", "0") or "0"))

            markets.append({
                "ticker": ticker,
                "title": (m.get("title") or "")[:70],
                "category": category,
                "final_price": final_price,
                "yes_won": yes_won,
                "volume": vol,
            })

    return markets


def bucket_analysis(markets: list[dict]) -> list[dict]:
    """Compute win rate by price bucket."""
    buckets = defaultdict(lambda: {"count": 0, "wins": 0})

    for m in markets:
        b = (m["final_price"] // BUCKET_SIZE) * BUCKET_SIZE
        buckets[b]["count"] += 1
        if m["yes_won"]:
            buckets[b]["wins"] += 1

    results = []
    for b in sorted(buckets.keys()):
        data = buckets[b]
        n = data["count"]
        wins = data["wins"]
        win_rate = wins / n if n > 0 else 0
        expected = (b + BUCKET_SIZE / 2) / 100  # midpoint of bucket

        # Binomial test
        p_value = _binomial_pvalue(wins, n, expected)

        results.append({
            "bucket": f"{b}-{b + BUCKET_SIZE}c",
            "bucket_low": b,
            "count": n,
            "wins": wins,
            "win_rate": round(win_rate * 100, 1),
            "expected": round(expected * 100, 1),
            "edge": round((win_rate - expected) * 100, 1),
            "p_value": round(p_value, 4),
            "significant": p_value < 0.05,
        })

    return results


def _binomial_pvalue(successes: int, trials: int, prob: float) -> float:
    """Two-sided binomial test p-value."""
    if trials == 0 or prob <= 0 or prob >= 1:
        return 1.0
    try:
        from scipy.stats import binomtest
        result = binomtest(successes, trials, prob, alternative="two-sided")
        return result.pvalue
    except ImportError:
        # Fallback: normal approximation for large N
        import math
        if trials < 10:
            return 1.0
        observed = successes / trials
        se = math.sqrt(prob * (1 - prob) / trials)
        if se == 0:
            return 1.0
        z = abs(observed - prob) / se
        # Two-sided p-value from z-score (approximation)
        p = math.erfc(z / math.sqrt(2))
        return min(p, 1.0)


def category_bucket_analysis(markets: list[dict]) -> dict[str, list[dict]]:
    """Bucket analysis split by category."""
    by_cat = defaultdict(list)
    for m in markets:
        by_cat[m["category"]].append(m)

    results = {}
    for cat, cat_markets in sorted(by_cat.items()):
        analysis = bucket_analysis(cat_markets)
        results[cat] = analysis

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi calibration study — mispricing analysis")
    parser.add_argument("--max-events", type=int, default=5000,
                        help="Max settled events to fetch (default: 5000)")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    client = KalshiClient(api_key, pk_path, PROD_BASE)

    print("=" * 70)
    print("KALSHI CALIBRATION STUDY")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Phase 1: Fetch settled events
    print(f"\n  Phase 1: Fetching settled events (max {args.max_events})...")
    events = fetch_settled_events(client, max_events=args.max_events)
    print(f"  Fetched {len(events)} settled events")

    # Phase 2: Extract resolved markets
    markets = extract_resolved_markets(events)
    print(f"  {len(markets)} resolved binary markets with valid final price")

    if not markets:
        print("\n  No resolved markets found. Check API response format.")
        # Debug: dump a sample event
        if events:
            sample = events[0]
            sample_markets = sample.get("markets", [])[:1]
            print(f"\n  Sample event category: {sample.get('category')}")
            if sample_markets:
                sm = sample_markets[0]
                print(f"  Sample market fields: {list(sm.keys())}")
                print(f"  result: {sm.get('result')}")
                for k in ("last_price_dollars", "previous_yes_bid_dollars",
                           "yes_bid_dollars", "previous_price_dollars",
                           "settlement_value_dollars"):
                    print(f"  {k}: {sm.get(k)}")
        return

    # Phase 3: Overall bucket analysis
    print(f"\n{'=' * 70}")
    print("OVERALL CALIBRATION")
    print(f"Resolved markets analyzed: {len(markets)}")
    print("=" * 70)

    overall = bucket_analysis(markets)
    header = (f"{'Bucket':<10} {'Count':>6} {'Win Rate':>9} {'Expected':>9} "
              f"{'Edge':>7} {'P-value':>8} {'Sig?':>4}")
    print(header)
    print("-" * len(header))
    for b in overall:
        sig = "***" if b["significant"] else ""
        print(f"{b['bucket']:<10} {b['count']:6d} {b['win_rate']:8.1f}% "
              f"{b['expected']:8.1f}% {b['edge']:+6.1f}% "
              f"{b['p_value']:8.4f} {sig:>4}")

    # Phase 4: By category (significant buckets only)
    print(f"\n{'=' * 70}")
    print("BY CATEGORY (buckets with p < 0.10 and N >= 10)")
    print("=" * 70)

    cat_results = category_bucket_analysis(markets)
    found_any = False
    for cat, buckets in cat_results.items():
        sig_buckets = [b for b in buckets
                       if b["p_value"] < 0.10 and b["count"] >= 10]
        if sig_buckets:
            found_any = True
            for b in sig_buckets:
                sig = "***" if b["p_value"] < 0.05 else "*"
                print(f"  {cat:<15} {b['bucket']:<10} N={b['count']:<4} "
                      f"win={b['win_rate']:.1f}% exp={b['expected']:.1f}% "
                      f"edge={b['edge']:+.1f}% p={b['p_value']:.4f} {sig}")

    if not found_any:
        print("  No significant mispricings found at p < 0.10")

    # Phase 5: Volume-weighted analysis for high-edge buckets
    edge_markets = [m for m in markets
                    if 55 <= m["final_price"] <= 75]
    if edge_markets:
        wins = sum(1 for m in edge_markets if m["yes_won"])
        n = len(edge_markets)
        wr = wins / n * 100
        exp = sum(m["final_price"] for m in edge_markets) / n
        print(f"\n  Focus: 55-75c bucket (Polymarket signal range)")
        print(f"  N={n}, win_rate={wr:.1f}%, expected={exp:.1f}%, "
              f"edge={wr - exp:+.1f}%")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "calibration_study.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_events": len(events),
            "total_markets": len(markets),
            "overall_calibration": overall,
            "category_calibration": cat_results,
            "markets": markets,
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
