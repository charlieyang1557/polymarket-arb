#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy A Phase 1: Calibration with timestamped closing prices.

Uses price_history.json which has backtest_price (typically price 24h
before market close) — NOT the stale lifetime lastTradePrice.

DATA CAVEAT: Polymarket GLOBAL data, not Polymarket US.

Usage:
    python3 scripts/strategy_a/phase1_closing_price.py
"""

import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from scripts.strategy_a.phase1_eda import (
    bucket_analysis, brier_score, simulate_pnl, plot_calibration,
)

OUTPUT_DIR = Path("data/strategy_a")
PRICE_HISTORY_PATH = "data/historical/price_history.json"

SPORTS_KEYWORDS = {
    "nba", "nfl", "nhl", "mlb", "ncaa", "cbb", "cfb", "wnba",
    "ufc", "mma", "boxing", "atp", "wta", "tennis",
    "epl", "ucl", "mls", "soccer", "premier league",
    "f1", "nascar", "golf", "pga", "cricket", "olympics",
}

SPORT_MAP = {
    "nba": "NBA", "wnba": "WNBA", "nfl": "NFL", "nhl": "NHL",
    "mlb": "MLB", "ncaa": "NCAA", "cbb": "NCAA", "cfb": "NCAA",
    "ufc": "UFC", "mma": "UFC", "boxing": "Boxing",
    "atp": "Tennis", "wta": "Tennis", "tennis": "Tennis",
    "epl": "Soccer", "ucl": "Soccer", "mls": "Soccer",
    "soccer": "Soccer", "premier league": "Soccer",
    "f1": "Motorsport", "nascar": "Motorsport",
    "golf": "Golf", "pga": "Golf",
    "cricket": "Cricket", "olympics": "Olympics",
}


def classify_sport(question: str, category_keywords: list) -> str | None:
    """Return sport name or None."""
    if "sports" in category_keywords:
        q = question.lower()
        for kw, sport in SPORT_MAP.items():
            if kw in q:
                return sport
        return "Other_sport"

    q = question.lower()
    for kw, sport in SPORT_MAP.items():
        if kw in q:
            return sport
    return None


def load_price_history(path: str) -> tuple[list[dict], list[dict]]:
    """Load price_history.json, split into sports and non-sports.

    Returns markets with fields: question, price_cents, outcome,
    volume, price_source, sport.
    """
    with open(path) as f:
        data = json.load(f)

    sports = []
    non_sports = []

    for entry_id, entry in data.items():
        bp = entry.get("backtest_price")
        if bp is None or bp <= 0 or bp >= 1:
            continue

        did_yes_win = entry.get("did_yes_win")
        if did_yes_win is None:
            continue

        price_cents = round(bp * 100)
        outcome = 1 if did_yes_win else 0
        volume = float(entry.get("volume", 0) or 0)
        question = entry.get("question", "")
        cats = entry.get("category_keywords", [])
        source = entry.get("price_source", "unknown")

        record = {
            "slug": entry_id,
            "question": question[:80],
            "price_cents": price_cents,
            "outcome": outcome,
            "volume": volume,
            "price_source": source,
        }

        sport = classify_sport(question, cats)
        if sport:
            record["sport"] = sport
            sports.append(record)
        else:
            record["sport"] = "non-sports"
            non_sports.append(record)

    return sports, non_sports


def main():
    print("=" * 70)
    print("STRATEGY A — PHASE 1: TIMESTAMPED CLOSING PRICES")
    print("=" * 70)
    print()
    print("  Price source: backtest_price from price_history.json")
    print("  Typically price_24h_before_close (88% of entries)")
    print("  DATA CAVEAT: Polymarket GLOBAL, not US")

    if not os.path.exists(PRICE_HISTORY_PATH):
        print(f"  FATAL: {PRICE_HISTORY_PATH} not found")
        sys.exit(1)

    # ---- Step 1: Load and filter ----
    print("\n[Step 1] Loading price history...")
    sports, non_sports = load_price_history(PRICE_HISTORY_PATH)
    all_markets = sports + non_sports

    print(f"  Total with valid backtest_price: {len(all_markets)}")
    print(f"  Sports: {len(sports)}")
    print(f"  Non-sports: {len(non_sports)}")

    # Price source breakdown
    sources = defaultdict(int)
    for m in all_markets:
        sources[m["price_source"]] += 1
    print(f"\n  Price source:")
    for s, c in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"    {s or 'None':20s}: {c:4d} ({100*c/len(all_markets):.1f}%)")

    # Sport breakdown
    sport_counts = defaultdict(int)
    for m in sports:
        sport_counts[m["sport"]] += 1
    print(f"\n  By sport:")
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        yes = sum(1 for m in sports if m["sport"] == sport and m["outcome"] == 1)
        pct = 100 * yes / count if count else 0
        print(f"    {sport:15s}: {count:4d} (YES wins: {pct:.1f}%)")

    # ---- Step 2: Price distribution ----
    print(f"\n[Step 2] Price distribution (sports only, n={len(sports)})")
    for lo in range(0, 100, 10):
        hi = lo + 10
        count = sum(1 for m in sports
                    if (lo < m["price_cents"] <= hi) or
                    (lo == 0 and 0 < m["price_cents"] <= hi))
        bar = "#" * min(40, count)
        print(f"    {lo:2d}-{hi:2d}c: {count:3d} {bar}")

    # YES win rate by bucket (quick check)
    print(f"\n  Quick sanity — YES win rate by price bucket:")
    for lo in range(0, 100, 20):
        hi = lo + 20
        in_b = [m for m in sports
                if (lo < m["price_cents"] <= hi) or
                (lo == 0 and 0 < m["price_cents"] <= hi)]
        if in_b:
            wr = sum(m["outcome"] for m in in_b) / len(in_b)
            print(f"    {lo:2d}-{hi:2d}c: n={len(in_b):3d}  YES_win_rate={wr:.1%}")

    # ---- Step 3: Calibration — sports only ----
    print("\n" + "=" * 70)
    print(f"[Step 3] CALIBRATION — SPORTS (n={len(sports)})")
    print("=" * 70)

    sports_buckets = bucket_analysis(sports)

    print(f"\n  {'Bucket':>8} {'N':>5} {'Implied':>8} {'Actual':>8} "
          f"{'Bias':>7} {'95% CI':>16} {'Sig':>4}")
    print(f"  {'-'*58}")

    flagged = []
    for b in sports_buckets:
        if b["n"] == 0:
            print(f"  {b['lo']:2d}-{b['hi']:2d}c     —")
            continue
        sig = ""
        if b["bias"] is not None and abs(b["bias"]) > 0.02 and b["n"] >= 30:
            sig = " ***"
            flagged.append(b)
        ci_str = f"[{b['ci_95_lo']:.3f}, {b['ci_95_hi']:.3f}]"
        print(f"  {b['lo']:2d}-{b['hi']:2d}c {b['n']:5d} "
              f"{b['implied_prob']:8.3f} {b['actual_win_rate']:8.3f} "
              f"{b['bias']:+7.3f} {ci_str:>16}{sig}")

    # ---- Sanity checks ----
    print(f"\n  SANITY CHECKS:")
    b4050 = next((b for b in sports_buckets if b["lo"] == 40), None)
    if b4050 and b4050["n"] > 0:
        wr = b4050["actual_win_rate"]
        status = "PASS" if 0.30 <= wr <= 0.70 else "FAIL"
        print(f"    40-50c bucket: n={b4050['n']}, win_rate={wr:.1%} [{status}]")
        if status == "FAIL":
            print(f"    WARNING: 40-50c should be ~50% — data may still be stale")
    else:
        print(f"    40-50c bucket: empty (insufficient data)")

    b090 = next((b for b in sports_buckets if b["lo"] == 90), None)
    if b090 and b090["n"] > 0:
        wr = b090["actual_win_rate"]
        status = "PASS" if wr >= 0.85 else "SUSPICIOUS"
        print(f"    90-100c bucket: n={b090['n']}, win_rate={wr:.1%} [{status}]")

    b010 = next((b for b in sports_buckets if b["lo"] == 0), None)
    if b010 and b010["n"] > 0:
        wr = b010["actual_win_rate"]
        status = "PASS" if wr <= 0.15 else "SUSPICIOUS"
        print(f"    0-10c bucket: n={b010['n']}, win_rate={wr:.1%} [{status}]")

    if flagged:
        print(f"\n  {len(flagged)} buckets with |bias| > 2% AND n >= 30:")
        for b in flagged:
            direction = "YES underpriced" if b["bias"] > 0 else "NO underpriced"
            print(f"    {b['lo']}-{b['hi']}c: {b['bias']:+.3f} "
                  f"({direction}, n={b['n']})")
    else:
        print(f"\n  No buckets with |bias| > 2% AND n >= 30")

    # ---- Brier Scores ----
    print(f"\n  Brier Scores:")
    brier_results = {}
    for strategy in ["market", "50pct", "fade_extremes"]:
        bs = brier_score(sports, strategy)
        brier_results[strategy] = bs
        print(f"    {strategy:20s}: {bs:.6f}")

    # ---- PnL simulation ----
    print(f"\n  PnL Simulation (IN-SAMPLE):")
    print(f"  {'Thresh':>7} {'Trades':>7} {'WinRate':>8} "
          f"{'NetPnL':>10} {'Edge/Tr':>8} {'Sharpe':>7}")
    print(f"  {'-'*50}")
    pnl_results = []
    for t in [0.01, 0.02, 0.03, 0.05, 0.07, 0.10]:
        r = simulate_pnl(sports, sports_buckets, t)
        pnl_results.append(r)
        print(f"  {t:7.0%} {r['num_trades']:7d} {r['win_rate']:8.1%} "
              f"{r['net_pnl']:10.1f}c {r['avg_edge']:8.2f}c "
              f"{r['sharpe']:7.2f}")

    # ---- Step 4: ALL markets calibration (for comparison) ----
    print(f"\n  ALL markets calibration (sports + non-sports, n={len(all_markets)}):")
    all_buckets = bucket_analysis(all_markets)
    for b in all_buckets:
        if b["n"] == 0:
            continue
        sig = " ***" if b["bias"] and abs(b["bias"]) > 0.02 and b["n"] >= 30 else ""
        print(f"    {b['lo']:2d}-{b['hi']:2d}c n={b['n']:5d} "
              f"impl={b['implied_prob']:.3f} actual={b['actual_win_rate']:.3f} "
              f"bias={b['bias']:+.3f}{sig}")

    # ---- Per-sport breakdown ----
    print(f"\n  Per-sport (buckets with n >= 10):")
    sport_breakdowns = {}
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        sport_markets = [m for m in sports if m["sport"] == sport]
        sb = bucket_analysis(sport_markets)
        sport_breakdowns[sport] = sb
        valid = [b for b in sb if b["n"] >= 10 and b["bias"] is not None]
        if valid:
            best = max(valid, key=lambda b: abs(b["bias"]))
            print(f"    {sport:15s}: n={count:4d} | "
                  f"{best['lo']}-{best['hi']}c bias={best['bias']:+.3f} "
                  f"(n={best['n']})")
        else:
            print(f"    {sport:15s}: n={count:4d} | no bucket with n>=10")

    # ---- Outputs ----
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        plot_calibration(sports_buckets,
                         str(OUTPUT_DIR / "calibration_curve_closing.png"))
    except Exception:
        print("  Plot generation failed")

    report = {
        "data_caveat": (
            "Polymarket GLOBAL (Gamma API). backtest_price = price 24h before close. "
            "NOT Polymarket US data."
        ),
        "sports_count": len(sports),
        "non_sports_count": len(non_sports),
        "sport_counts": dict(sport_counts),
        "sports_calibration": sports_buckets,
        "all_calibration": all_buckets,
        "brier_scores": brier_results,
        "pnl_simulations": pnl_results,
        "sport_breakdowns": sport_breakdowns,
        "flagged_buckets": [
            {"range": f"{b['lo']}-{b['hi']}c", "bias": b["bias"], "n": b["n"]}
            for b in flagged
        ],
    }
    report_path = OUTPUT_DIR / "phase1_closing_price_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Saved: {report_path}")

    # ---- Decision ----
    print("\n" + "=" * 70)
    print("DECISION GATE — CLOSING PRICES")
    print("=" * 70)
    print(f"  Total sports markets: {len(sports)}")
    print(f"  Price source: backtest_price (24h before close for 88%)")

    if flagged:
        print(f"  BIAS DETECTED: {len(flagged)} buckets with |bias|>2%, n>=30")
        positive = [r for r in pnl_results if r["net_pnl"] > 0]
        if positive:
            best = max(positive, key=lambda r: r["net_pnl"])
            print(f"  Best PnL: {best['net_pnl']:.0f}c at {best['threshold']:.0%}")
        print(f"  BUT: Only {len(sports)} sports markets — underpowered.")
        print(f"  AND: Polymarket Global ≠ Polymarket US.")
    else:
        print(f"  NO significant bias with timestamped closing prices.")
        print(f"  The previous 'signal' was entirely a stale-price artifact.")
        print(f"  n={len(sports)} may be too small — need more data before killing.")


if __name__ == "__main__":
    main()
