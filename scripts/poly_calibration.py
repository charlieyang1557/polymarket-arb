#!/usr/bin/env python3
"""
Polymarket US Calibration Study — test for systematic mispricing.

Fetches resolved markets via polymarket-us SDK, buckets by last trade price,
checks whether actual win rates diverge from implied probability.

PUBLIC endpoints only — no auth needed.

Usage:
    python scripts/poly_calibration.py
    python scripts/poly_calibration.py --max-markets 2000 --min-shares 100
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from polymarket_us import PolymarketUS

OUTPUT_DIR = Path("data/polymarket_diagnostic")
BUCKET_SIZE = 10  # cents


# ---------------------------------------------------------------------------
# Pure helpers (tested)
# ---------------------------------------------------------------------------

def _binomial_pvalue(successes: int, trials: int, prob: float) -> float:
    """Two-sided binomial test p-value."""
    if trials == 0 or prob <= 0 or prob >= 1:
        return 1.0
    try:
        from scipy.stats import binomtest
        result = binomtest(successes, trials, prob, alternative="two-sided")
        return result.pvalue
    except ImportError:
        if trials < 10:
            return 1.0
        observed = successes / trials
        se = math.sqrt(prob * (1 - prob) / trials)
        if se == 0:
            return 1.0
        z = abs(observed - prob) / se
        p = math.erfc(z / math.sqrt(2))
        return min(p, 1.0)


def extract_resolved_markets(enriched_markets: list[dict],
                              min_shares: int = 0) -> list[dict]:
    """Extract resolved markets with final price and outcome.

    Each market dict must already have enrichment fields:
      _settlement: 1 (long won) or 0 (short won) or None
      _last_trade_price: float (0-1)
      _shares_traded: float
    """
    results = []

    for m in enriched_markets:
        settlement = m.get("_settlement")
        if settlement is None:
            continue

        ltp = m.get("_last_trade_price")
        if ltp is None:
            continue
        try:
            price = float(ltp)
        except (ValueError, TypeError):
            continue

        if price <= 0 or price >= 1.0:
            continue

        final_price = int(round(price * 100))
        if final_price <= 0 or final_price >= 100:
            continue

        shares = float(m.get("_shares_traded", 0) or 0)
        if shares < min_shares:
            continue

        long_won = settlement == 1

        results.append({
            "slug": m.get("slug", ""),
            "question": (m.get("question") or "")[:80],
            "final_price": final_price,
            "long_won": long_won,
            "shares_traded": shares,
            "market_type": m.get("marketType", ""),
            "series_slug": m.get("seriesSlug", ""),
            "category": m.get("category", ""),
        })

    return results


def bucket_analysis(markets: list[dict]) -> list[dict]:
    """Compute win rate by price bucket."""
    buckets = defaultdict(lambda: {"count": 0, "wins": 0})

    for m in markets:
        b = (m["final_price"] // BUCKET_SIZE) * BUCKET_SIZE
        buckets[b]["count"] += 1
        if m["long_won"]:
            buckets[b]["wins"] += 1

    results = []
    for b in sorted(buckets.keys()):
        data = buckets[b]
        n = data["count"]
        wins = data["wins"]
        win_rate = wins / n if n > 0 else 0
        expected = (b + BUCKET_SIZE / 2) / 100

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


def series_breakdown(markets: list[dict]) -> dict[str, list[dict]]:
    """Bucket analysis split by series (league)."""
    by_series = defaultdict(list)
    for m in markets:
        by_series[m.get("series_slug", "unknown")].append(m)

    results = {}
    for series, s_markets in sorted(by_series.items()):
        analysis = bucket_analysis(s_markets)
        if analysis:
            results[series] = analysis

    return results


def focus_range_analysis(markets: list[dict], low: int = 60,
                          high: int = 70) -> dict:
    """Focused analysis for a specific price range."""
    focus = [m for m in markets if low <= m["final_price"] < high]
    n = len(focus)
    if n == 0:
        return {"n": 0, "win_rate": 0, "expected": 0, "edge": 0, "p_value": 1.0}

    wins = sum(1 for m in focus if m["long_won"])
    win_rate = round(wins / n * 100, 1)
    expected = round(sum(m["final_price"] for m in focus) / n, 1)
    edge = round(win_rate - expected, 1)
    p_value = round(_binomial_pvalue(wins, n, expected / 100), 4)

    return {"n": n, "wins": wins, "win_rate": win_rate,
            "expected": expected, "edge": edge, "p_value": p_value}


# ---------------------------------------------------------------------------
# Main (SDK-dependent)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket US calibration study")
    parser.add_argument("--max-markets", type=int, default=3000,
                        help="Max closed markets to fetch (default: 3000)")
    parser.add_argument("--min-shares", type=int, default=50,
                        help="Min shares traded to include (default: 50)")
    args = parser.parse_args()

    client = PolymarketUS()

    print("=" * 70)
    print("POLYMARKET US — CALIBRATION STUDY")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Max markets: {args.max_markets} | Min shares: {args.min_shares}")
    print("=" * 70)

    # Phase 1: Fetch closed markets with pagination
    print(f"\n  Phase 1: Fetching closed markets...")
    all_markets = []
    offset = 0
    page_size = 100  # SDK limit

    while len(all_markets) < args.max_markets:
        resp = client.markets.list({
            "limit": page_size,
            "offset": offset,
            "closed": True,
            "orderBy": ["volume"],
            "orderDirection": "desc",
        })
        batch = resp.get("markets", [])
        if not batch:
            break

        all_markets.extend(batch)
        offset += page_size
        print(f"    ... {len(all_markets)} markets fetched")
        time.sleep(0.1)

    print(f"  Total closed markets: {len(all_markets)}")

    # Phase 2: Enrich with settlement + BBO data
    print(f"\n  Phase 2: Enriching with settlement + BBO...")
    enriched = []
    errors = 0

    for i, m in enumerate(all_markets):
        slug = m.get("slug", "")
        if not slug:
            continue

        try:
            settle = client.markets.settlement(slug)
            settlement_val = settle.get("settlement")
        except Exception:
            settlement_val = None
            errors += 1

        try:
            bbo = client.markets.bbo(slug)
            md = bbo.get("marketData", {}) if bbo else {}
            ltp = md.get("lastTradePx", {})
            last_price = float(ltp.get("value", 0)) if ltp else 0
            shares = float(md.get("sharesTraded", "0") or "0")
        except Exception:
            last_price = 0
            shares = 0
            errors += 1

        m["_settlement"] = settlement_val
        m["_last_trade_price"] = last_price
        m["_shares_traded"] = shares
        enriched.append(m)

        if (i + 1) % 100 == 0:
            print(f"    ... {i + 1}/{len(all_markets)} ({errors} errors)")

        time.sleep(0.05)  # rate limiting

    print(f"  Enriched {len(enriched)} markets ({errors} API errors)")

    # Phase 3: Extract resolved
    markets = extract_resolved_markets(enriched, min_shares=args.min_shares)
    print(f"  {len(markets)} resolved markets with valid price + shares >= {args.min_shares}")

    if not markets:
        print("\n  No resolved markets found.")
        return

    # Phase 4: Overall bucket analysis
    print(f"\n{'=' * 70}")
    print(f"OVERALL CALIBRATION ({len(markets)} markets)")
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

    # Phase 5: By series (league)
    print(f"\n{'=' * 70}")
    print("BY SERIES (buckets with p < 0.10 and N >= 10)")
    print("=" * 70)

    sr = series_breakdown(markets)
    found = False
    for series, buckets in sr.items():
        n_total = sum(b["count"] for b in buckets)
        sig_buckets = [b for b in buckets
                       if b["p_value"] < 0.10 and b["count"] >= 10]
        if sig_buckets:
            found = True
            print(f"\n  {series} (N={n_total}):")
            for b in sig_buckets:
                sig = "***" if b["p_value"] < 0.05 else "*"
                print(f"    {b['bucket']:<10} N={b['count']:<4} "
                      f"win={b['win_rate']:.1f}% exp={b['expected']:.1f}% "
                      f"edge={b['edge']:+.1f}% p={b['p_value']:.4f} {sig}")

    if not found:
        print("  No significant mispricings found at p < 0.10")

    # Phase 6: By market type
    print(f"\n{'=' * 70}")
    print("BY MARKET TYPE")
    print("=" * 70)

    by_type = defaultdict(list)
    for m in markets:
        by_type[m.get("market_type", "unknown")].append(m)

    for mt, mt_markets in sorted(by_type.items()):
        n = len(mt_markets)
        wins = sum(1 for m in mt_markets if m["long_won"])
        wr = wins / n * 100
        exp = sum(m["final_price"] for m in mt_markets) / n
        print(f"  {mt:>12}: N={n:<5} win_rate={wr:.1f}% "
              f"expected={exp:.1f}% edge={wr - exp:+.1f}%")

    # Phase 7: Focus ranges
    print(f"\n{'=' * 70}")
    print("FOCUS RANGES")
    print("=" * 70)

    for low, high, label in [
        (60, 70, "60-70c (global API had 81.4% win rate, p=0.008)"),
        (55, 75, "55-75c (broad signal range)"),
        (40, 60, "40-60c (underdog range)"),
    ]:
        focus = focus_range_analysis(markets, low=low, high=high)
        if focus["n"] > 0:
            sig = "***" if focus["p_value"] < 0.05 else (
                  "*" if focus["p_value"] < 0.10 else "")
            print(f"\n  {label}")
            print(f"    N={focus['n']}, win_rate={focus['win_rate']:.1f}%, "
                  f"expected={focus['expected']:.1f}%, edge={focus['edge']:+.1f}%")
            print(f"    P-value: {focus['p_value']:.4f} {sig}")
        else:
            print(f"\n  {label}: no markets")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "calibration_study.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_closed": len(all_markets),
            "total_resolved": len(markets),
            "min_shares": args.min_shares,
            "overall_calibration": overall,
            "series_breakdown": sr,
            "focus_60_70": focus_range_analysis(markets, 60, 70),
            "focus_55_75": focus_range_analysis(markets, 55, 75),
            "by_market_type": {
                mt: {"n": len(mm),
                     "win_rate": round(sum(1 for m in mm if m["long_won"]) / len(mm) * 100, 1),
                     "avg_price": round(sum(m["final_price"] for m in mm) / len(mm), 1)}
                for mt, mm in by_type.items()
            },
            "markets": markets,
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
