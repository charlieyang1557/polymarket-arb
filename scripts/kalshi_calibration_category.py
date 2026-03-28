#!/usr/bin/env python3
"""
Calibration deep dive: category breakdown + timing analysis.

Builds on calibration_verify.py — breaks down the mispricing signal by
category and checks whether Sports edge is a live-game artifact.

Usage:
    python scripts/kalshi_calibration_category.py
    python scripts/kalshi_calibration_category.py --max-events 5000 --sample-trades 200
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
from scripts.kalshi_calibration import (
    fetch_settled_events, bucket_analysis, _binomial_pvalue,
)
from scripts.kalshi_calibration_verify import extract_markets_with_timestamps

OUTPUT_DIR = Path("data/kalshi_diagnostic")
BUCKET_SIZE = 10


# ---- Pure functions (tested) ----

def category_filtered_analysis(markets: list[dict],
                                min_volume: int = 100) -> dict[str, list[dict]]:
    """Per-category bucket analysis, filtered by volume."""
    by_cat = defaultdict(list)
    for m in markets:
        if m["volume"] >= min_volume:
            by_cat[m["category"]].append(m)

    results = {}
    for cat, cat_markets in sorted(by_cat.items()):
        analysis = bucket_analysis(cat_markets)
        if analysis:
            results[cat] = analysis

    return results


def significant_summary(cat_results: dict[str, list[dict]],
                        min_n: int = 20, max_p: float = 0.05) -> list[dict]:
    """Extract only buckets with N >= min_n and p < max_p."""
    sig = []
    for cat, buckets in cat_results.items():
        for b in buckets:
            if b["count"] >= min_n and b["p_value"] < max_p:
                sig.append({"category": cat, **b})
    return sig


def compute_timing_gaps(markets: list[dict]) -> list[dict]:
    """Compute close-to-settlement and last_trade-to-close gaps."""
    results = []
    for m in markets:
        close_str = m.get("close_time") or ""
        last_trade_str = m.get("last_trade_time") or ""
        settlement_str = m.get("settlement_ts") or ""

        close_ts = _parse_ts(close_str)
        last_trade_ts = _parse_ts(last_trade_str)
        settlement_ts = _parse_ts(settlement_str)

        c2s = None
        lt2c = None
        within_30 = None

        if close_ts and settlement_ts:
            c2s = (settlement_ts - close_ts).total_seconds() / 3600

        if last_trade_ts and close_ts:
            lt2c = (close_ts - last_trade_ts).total_seconds() / 3600
            within_30 = lt2c <= 0.5  # 30 min

        results.append({
            "ticker": m.get("ticker", ""),
            "category": m.get("category", ""),
            "final_price": m.get("final_price"),
            "close_to_settlement_hours": c2s,
            "last_trade_to_close_hours": lt2c,
            "trade_within_30min_of_close": within_30,
        })

    return results


def sample_anomalous_tickers(markets: list[dict], category: str,
                              bucket_low: int, bucket_high: int,
                              max_samples: int = 10) -> list[dict]:
    """Return up to max_samples tickers from a specific category+bucket."""
    matches = [
        m for m in markets
        if m["category"] == category
        and bucket_low <= m["final_price"] < bucket_high
    ]
    return matches[:max_samples]


def sports_focus_analysis(markets: list[dict],
                           min_volume: int = 100) -> dict:
    """Sports 55-75c filtered analysis with edge and p-value."""
    focus = [
        m for m in markets
        if m["category"] == "Sports"
        and 55 <= m["final_price"] <= 75
        and m["volume"] >= min_volume
    ]
    n = len(focus)
    if n == 0:
        return {"n": 0, "win_rate": 0, "expected": 0, "edge": 0, "p_value": 1.0}

    wins = sum(1 for m in focus if m["yes_won"])
    win_rate = round(wins / n * 100, 1)
    expected = round(sum(m["final_price"] for m in focus) / n, 1)
    edge = round(win_rate - expected, 1)
    p_value = round(_binomial_pvalue(wins, n, expected / 100), 4)

    return {"n": n, "wins": wins, "win_rate": win_rate,
            "expected": expected, "edge": edge, "p_value": p_value}


# ---- Helpers ----

def _parse_ts(ts_str: str):
    """Parse ISO timestamp, return datetime or None."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---- Main (API-dependent) ----

def _fetch_settlement_ts(client: KalshiClient, markets: list[dict]):
    """Enrich markets with settlement_ts from individual market lookups."""
    # Only fetch for Sports markets in 40-80c range (where timing matters)
    targets = [m for m in markets
                if m["category"] == "Sports"
                and 40 <= m["final_price"] <= 80]

    print(f"\n  Fetching settlement timestamps for {len(targets)} "
          f"Sports markets (40-80c)...")

    for i, m in enumerate(targets):
        try:
            full = client.get_market(m["ticker"])
            market = full.get("market", full)
            m["settlement_ts"] = (market.get("settlement_timer_seconds")
                                   or market.get("close_time")
                                   or market.get("expected_expiration_time")
                                   or "")
            # Also grab actual close_time if richer
            if market.get("close_time"):
                m["close_time"] = market["close_time"]
            time.sleep(0.06)
        except Exception as e:
            m["settlement_ts"] = ""
        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(targets)}")


def _fetch_last_trades(client: KalshiClient, markets: list[dict],
                        sample_size: int = 200):
    """Fetch last trade time for Sports markets in 40-80c range."""
    targets = [m for m in markets
                if m["category"] == "Sports"
                and 40 <= m["final_price"] <= 80
                and m.get("last_trade_time") is None]

    import random
    random.seed(42)
    sample = random.sample(targets, min(sample_size, len(targets)))

    print(f"\n  Fetching last trades for {len(sample)} Sports 40-80c markets...")

    for i, m in enumerate(sample):
        try:
            trade_data = client.get_trades(m["ticker"], limit=1)
            trades = trade_data.get("trades", [])
            if trades:
                m["last_trade_time"] = trades[0].get("created_time", "")
            time.sleep(0.06)
        except Exception:
            pass
        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(sample)}")


def main():
    parser = argparse.ArgumentParser(
        description="Calibration category + timing analysis")
    parser.add_argument("--max-events", type=int, default=5000,
                        help="Max settled events to fetch (default: 5000)")
    parser.add_argument("--sample-trades", type=int, default=200,
                        help="Sports markets to fetch last trades for (default: 200)")
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
    print("KALSHI CALIBRATION — CATEGORY + TIMING DEEP DIVE")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Phase 1: Fetch data
    print(f"\n  Fetching settled events (max {args.max_events})...")
    events = fetch_settled_events(client, max_events=args.max_events)
    print(f"  Fetched {len(events)} settled events")

    markets = extract_markets_with_timestamps(events)
    print(f"  Extracted {len(markets)} resolved markets")

    if not markets:
        print("  No markets to analyze.")
        return

    # Phase 2: Per-category filtered analysis
    print(f"\n{'=' * 70}")
    print(f"PER-CATEGORY CALIBRATION (volume >= {args.min_volume})")
    print("=" * 70)

    cat_results = category_filtered_analysis(markets, min_volume=args.min_volume)

    for cat, buckets in cat_results.items():
        n_total = sum(b["count"] for b in buckets)
        print(f"\n  Category: {cat} ({n_total} filtered markets)")
        header = f"  {'Bucket':<10} {'Count':>6} {'Win Rate':>9} {'Expected':>9} {'Edge':>7} {'P-value':>8}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for b in buckets:
            sig = "***" if b["p_value"] < 0.05 else (
                  "*" if b["p_value"] < 0.10 else "")
            print(f"  {b['bucket']:<10} {b['count']:6d} {b['win_rate']:8.1f}% "
                  f"{b['expected']:8.1f}% {b['edge']:+6.1f}% "
                  f"{b['p_value']:8.4f} {sig}")

    # Phase 3: Significant summary
    print(f"\n{'=' * 70}")
    print("SIGNIFICANT BUCKETS (N >= 20, p < 0.05)")
    print("=" * 70)

    sig = significant_summary(cat_results, min_n=20, max_p=0.05)
    if sig:
        for s in sig:
            print(f"  {s['category']:<15} {s['bucket']:<10} N={s['count']:<4} "
                  f"edge={s['edge']:+.1f}% p={s['p_value']:.4f}")
    else:
        print("  No significant mispricings at N>=20, p<0.05")

    # Phase 4: Sports timing analysis
    print(f"\n{'=' * 70}")
    print("SPORTS TIMING ANALYSIS (pre-game vs live artifact check)")
    print("=" * 70)

    _fetch_last_trades(client, markets, sample_size=args.sample_trades)

    # For settlement timestamp, use close_time + expected_expiration_time
    # already captured in extract_markets_with_timestamps

    sports_40_80 = [m for m in markets
                     if m["category"] == "Sports"
                     and 40 <= m["final_price"] <= 80
                     and m["volume"] >= args.min_volume]

    timing = compute_timing_gaps(sports_40_80)
    timing_with_data = [t for t in timing
                         if t["last_trade_to_close_hours"] is not None]

    if timing_with_data:
        lt2c_vals = sorted(t["last_trade_to_close_hours"]
                           for t in timing_with_data)
        c2s_vals = [t["close_to_settlement_hours"]
                    for t in timing_with_data
                    if t["close_to_settlement_hours"] is not None]

        n_within_30 = sum(1 for t in timing_with_data
                          if t["trade_within_30min_of_close"])
        pct_within_30 = n_within_30 / len(timing_with_data) * 100

        median_lt2c = lt2c_vals[len(lt2c_vals) // 2]

        print(f"\n  Sports 40-80c filtered (volume >= {args.min_volume}):")
        print(f"    N markets with trade data: {len(timing_with_data)}")
        print(f"    Median last_trade-to-close gap: {median_lt2c:.2f}h")
        if c2s_vals:
            c2s_sorted = sorted(c2s_vals)
            median_c2s = c2s_sorted[len(c2s_sorted) // 2]
            print(f"    Median close-to-settlement gap: {median_c2s:.2f}h")
        print(f"    % last trade within 30min of close: {pct_within_30:.0f}%")

        # Distribution of last_trade_to_close gaps
        print(f"\n    Last-trade-to-close distribution:")
        for threshold_h, label in [(0.5, "<30min"), (1, "<1h"),
                                     (2, "<2h"), (6, "<6h"), (24, "<24h")]:
            count = sum(1 for v in lt2c_vals if v <= threshold_h)
            pct = count / len(lt2c_vals) * 100
            print(f"      {label:>8}: {pct:5.1f}% ({count}/{len(lt2c_vals)})")

        # Interpretation
        if pct_within_30 > 70:
            print(f"\n    INTERPRETATION: {pct_within_30:.0f}% of last trades "
                  f"within 30min of close")
            print(f"    → Price was LIVE at close — NOT a frozen relic")
            print(f"    → If markets close at game start, 60c IS the "
                  f"pre-game consensus")
        elif pct_within_30 < 30:
            print(f"\n    INTERPRETATION: Only {pct_within_30:.0f}% within 30min")
            print(f"    → Many prices are STALE — frozen relic hypothesis "
                  f"supported")
        else:
            print(f"\n    INTERPRETATION: Mixed — {pct_within_30:.0f}% "
                  f"within 30min")
            print(f"    → Need per-bucket breakdown to separate stale vs live")
    else:
        print("  No timing data available for Sports markets")

    # Phase 5: Anomalous non-Sports buckets
    print(f"\n{'=' * 70}")
    print("ANOMALOUS NON-SPORTS BUCKETS — SAMPLE TICKERS")
    print("=" * 70)

    anomalous_targets = [
        ("Financials", 30, 40),
        ("Crypto", 70, 80),
        ("Climate", 60, 70),
    ]
    for cat, blo, bhi in anomalous_targets:
        samples = sample_anomalous_tickers(markets, cat, blo, bhi)
        if samples:
            print(f"\n  {cat} {blo}-{bhi}c ({len(samples)} samples):")
            for s in samples:
                print(f"    {s['ticker']:<30} {s['title'][:50]:<50} "
                      f"vol={s['volume']}")
        else:
            print(f"\n  {cat} {blo}-{bhi}c: no markets found")

    # Phase 6: Sports 55-75c focus
    print(f"\n{'=' * 70}")
    print("SPORTS 55-75c FOCUS (primary trade candidate)")
    print("=" * 70)

    focus = sports_focus_analysis(markets, min_volume=args.min_volume)
    if focus["n"] > 0:
        print(f"  N={focus['n']}, win_rate={focus['win_rate']:.1f}%, "
              f"expected={focus['expected']:.1f}%")
        print(f"  Edge: {focus['edge']:+.1f}%")
        print(f"  P-value: {focus['p_value']:.4f}")
        if focus["p_value"] < 0.05:
            print(f"  *** SIGNIFICANT at p<0.05 ***")
        else:
            print(f"  Not significant (p >= 0.05)")
    else:
        print("  No Sports markets in 55-75c filtered range")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "calibration_categories.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_markets": len(markets),
            "min_volume_filter": args.min_volume,
            "category_calibration": cat_results,
            "significant_buckets": sig,
            "sports_timing": {
                "n_with_data": len(timing_with_data) if timing_with_data else 0,
                "median_last_trade_to_close_hours": (
                    median_lt2c if timing_with_data else None),
                "pct_within_30min": (
                    round(pct_within_30, 1) if timing_with_data else None),
                "gaps": timing_with_data[:50] if timing_with_data else [],
            },
            "sports_focus_55_75": focus,
            "anomalous_samples": {
                f"{cat}_{blo}_{bhi}": sample_anomalous_tickers(markets, cat, blo, bhi)
                for cat, blo, bhi in anomalous_targets
            },
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
