#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy A Phase 1 Re-analysis: Sports-Only Filter.

IMPORTANT DATA CAVEAT:
  raw_markets.json is from the Gamma API = Polymarket GLOBAL (polymarket.com)
  We trade on Polymarket US (polymarket.us) — a completely different platform
  with different markets, users, and pricing dynamics.
  This analysis can only tell us if bias EXISTS in prediction markets generally.
  It CANNOT directly predict Polymarket US edge. Phase 2+ must use US data.

Filters to sports-only markets, investigates lastTradePrice quality,
and re-runs calibration to see if the uniform negative bias was a
crypto/other data artifact.

Usage:
    python3 scripts/strategy_a/phase1_sports_only.py
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from scripts.strategy_a.phase1_eda import (
    bucket_analysis, brier_score, simulate_pnl, taker_fee_cents,
    plot_calibration, SLIPPAGE_CENTS,
)

OUTPUT_DIR = Path("data/strategy_a")
DATA_PATH = "data/historical/raw_markets.json"

# Sports keywords for filtering
SPORTS_KEYWORDS = {
    "nba", "nfl", "nhl", "mlb", "ncaa", "cbb", "cfb", "wnba",
    "ufc", "mma", "boxing",
    "atp", "wta", "tennis",
    "epl", "ucl", "mls", "premier league", "champions league",
    "fifa", "soccer", "football",  # football = soccer in global context
    "f1", "nascar", "motorsport", "formula",
    "golf", "pga",
    "cricket", "ipl",
    "olympics",
}

# Exclude keywords (even if slug matches a sport keyword)
EXCLUDE_KEYWORDS = {
    "updown", "crypto", "bitcoin", "btc", "eth", "xrp", "solana",
    "doge", "will-the-price", "election", "president", "congress",
    "senate", "governor", "who-will-win-the-2", "emmy", "oscar",
    "grammy", "weather", "temperature",
}

SPORT_MAP = {
    "nba": "NBA", "wnba": "WNBA",
    "nfl": "NFL",
    "nhl": "NHL",
    "mlb": "MLB",
    "ncaa": "NCAA", "cbb": "NCAA", "cfb": "NCAA",
    "ufc": "UFC", "mma": "UFC", "boxing": "Boxing",
    "atp": "Tennis", "wta": "Tennis", "tennis": "Tennis",
    "epl": "Soccer", "ucl": "Soccer", "mls": "Soccer",
    "premier league": "Soccer", "champions league": "Soccer",
    "fifa": "Soccer", "soccer": "Soccer",
    "f1": "Motorsport", "nascar": "Motorsport",
    "formula": "Motorsport", "motorsport": "Motorsport",
    "golf": "Golf", "pga": "Golf",
    "cricket": "Cricket", "ipl": "Cricket",
    "olympics": "Olympics",
}


def classify_sport(slug: str, question: str) -> str | None:
    """Return sport name if sports market, None if not."""
    text = (slug + " " + question).lower()

    # Exclude first
    for kw in EXCLUDE_KEYWORDS:
        if kw in text:
            return None

    # Match sport
    for kw, sport in SPORT_MAP.items():
        if kw in text:
            return sport

    return None


def load_and_filter(path: str) -> tuple[list[dict], list[dict]]:
    """Load raw markets, return (sports, non_sports) with normalized fields."""
    with open(path) as f:
        raw = json.load(f)

    sports = []
    non_sports = []

    for m in raw:
        # Parse settlement
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(prices, list) or len(prices) != 2:
            continue
        try:
            p0, p1 = float(prices[0]), float(prices[1])
        except (ValueError, TypeError):
            continue
        if p0 == 1 and p1 == 0:
            outcome = 1
        elif p0 == 0 and p1 == 1:
            outcome = 0
        else:
            continue

        ltp = m.get("lastTradePrice")
        if ltp is None or ltp == 0:
            continue
        try:
            price_frac = float(ltp)
        except (ValueError, TypeError):
            continue
        if price_frac <= 0 or price_frac >= 1:
            continue

        price_cents = round(price_frac * 100)

        record = {
            "slug": m.get("slug", ""),
            "question": (m.get("question") or "")[:80],
            "price_cents": price_cents,
            "outcome": outcome,
            "volume": float(m.get("volumeNum", 0) or 0),
            "closed_time": m.get("closedTime", ""),
            "end_date": m.get("endDate", ""),
            "updated_at": m.get("updatedAt", ""),
            "created_at": m.get("createdAt", ""),
        }

        sport = classify_sport(m.get("slug", ""), m.get("question", ""))
        if sport:
            record["sport"] = sport
            sports.append(record)
        else:
            record["sport"] = "non-sports"
            non_sports.append(record)

    return sports, non_sports


def analyze_price_quality(markets: list[dict]):
    """Investigate lastTradePrice distribution and staleness."""
    print("\n[Step 2] lastTradePrice quality analysis")

    prices = [m["price_cents"] for m in markets]
    n = len(prices)

    # Distribution
    near_0 = sum(1 for p in prices if p <= 5)
    near_100 = sum(1 for p in prices if p >= 95)
    mid_range = sum(1 for p in prices if 20 <= p <= 80)
    extreme = near_0 + near_100

    print(f"  Total sports markets: {n}")
    print(f"  Price distribution:")
    print(f"    Near 0 (<=5c):    {near_0:6d} ({100*near_0/n:.1f}%)")
    print(f"    Near 100 (>=95c): {near_100:6d} ({100*near_100/n:.1f}%)")
    print(f"    Mid-range (20-80c): {mid_range:5d} ({100*mid_range/n:.1f}%)")
    print(f"    Extreme (<=5 or >=95): {extreme:5d} ({100*extreme/n:.1f}%)")

    # Price histogram (10c buckets)
    print(f"\n  Price histogram:")
    for lo in range(0, 100, 10):
        hi = lo + 10
        count = sum(1 for p in prices if lo < p <= hi)
        if lo == 0:
            count = sum(1 for p in prices if 0 < p <= hi)
        bar = "#" * min(50, count // max(1, n // 500))
        print(f"    {lo:2d}-{hi:2d}c: {count:5d} {bar}")

    # Staleness analysis using closedTime
    print(f"\n  Price staleness (closedTime vs endDate):")
    gaps = []
    for m in markets:
        ct = m.get("closed_time", "")
        ed = m.get("end_date", "")
        if not ct or not ed:
            continue
        try:
            closed = datetime.fromisoformat(ct.replace("+00", "+00:00")
                                            .replace(" ", "T"))
            end = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            gap_hours = (end - closed).total_seconds() / 3600
            if -720 < gap_hours < 720:  # within 30 days
                gaps.append(gap_hours)
        except (ValueError, TypeError):
            continue

    if gaps:
        gaps.sort()
        print(f"    Markets with timing data: {len(gaps)}")
        print(f"    Median gap (endDate - closedTime): {gaps[len(gaps)//2]:.1f}h")
        print(f"    Mean gap: {sum(gaps)/len(gaps):.1f}h")

        # Distribution of gaps
        buckets_gap = [
            (-999, 0, "Closed AFTER endDate"),
            (0, 1, "< 1h before end"),
            (1, 24, "1-24h before end"),
            (24, 168, "1-7 days before end"),
            (168, 999, "> 7 days before end"),
        ]
        for lo, hi, label in buckets_gap:
            count = sum(1 for g in gaps if lo <= g < hi)
            print(f"    {label:25s}: {count:5d} ({100*count/len(gaps):.1f}%)")
    else:
        print("    No timing data available")


def run_calibration(markets: list[dict], label: str) -> list[dict]:
    """Run calibration analysis and print results."""
    buckets = bucket_analysis(markets)

    print(f"\n  {'Bucket':>8} {'N':>7} {'Implied':>8} {'Actual':>8} "
          f"{'Bias':>7} {'95% CI':>16} {'Sig':>4}")
    print(f"  {'-'*62}")

    flagged = []
    for b in buckets:
        if b["n"] == 0:
            print(f"  {b['lo']:2d}-{b['hi']:2d}c {'—':>7}")
            continue
        sig = ""
        if b["bias"] is not None and abs(b["bias"]) > 0.02 and b["n"] >= 30:
            sig = " ***"
            flagged.append(b)
        ci_str = f"[{b['ci_95_lo']:.3f}, {b['ci_95_hi']:.3f}]"
        print(f"  {b['lo']:2d}-{b['hi']:2d}c {b['n']:7d} "
              f"{b['implied_prob']:8.3f} {b['actual_win_rate']:8.3f} "
              f"{b['bias']:+7.3f} {ci_str:>16}{sig}")

    if flagged:
        print(f"\n  {len(flagged)} buckets with |bias| > 2% AND n >= 30:")
        for b in flagged:
            direction = "YES underpriced" if b["bias"] > 0 else "NO underpriced"
            print(f"    {b['lo']}-{b['hi']}c: {b['bias']:+.3f} "
                  f"({direction}, n={b['n']})")

    return buckets


def main():
    print("=" * 70)
    print("STRATEGY A — PHASE 1 RE-ANALYSIS: SPORTS-ONLY")
    print("=" * 70)
    print()
    print("  DATA CAVEAT: raw_markets.json = Polymarket GLOBAL (polymarket.com)")
    print("  We trade Polymarket US (polymarket.us) — DIFFERENT platform.")
    print("  This analysis tests if bias exists in prediction markets generally.")
    print("  It cannot directly predict Polymarket US edge.")

    # ---- Step 1: Filter to sports ----
    print("\n[Step 1] Filtering to sports-only markets...")
    sports, non_sports = load_and_filter(DATA_PATH)

    print(f"  Sports markets:     {len(sports):,d}")
    print(f"  Non-sports markets: {len(non_sports):,d}")
    print(f"  Sports fraction:    {100*len(sports)/(len(sports)+len(non_sports)):.1f}%")

    # By sport
    sport_counts = defaultdict(int)
    for m in sports:
        sport_counts[m["sport"]] += 1
    print(f"\n  By sport:")
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        yes = sum(1 for m in sports if m["sport"] == sport and m["outcome"] == 1)
        print(f"    {sport:12s}: {count:5d} "
              f"(YES wins: {100*yes/count:.1f}%)")

    # ---- Step 2: Price quality ----
    analyze_price_quality(sports)

    # ---- Step 3: Calibration — sports only ----
    print("\n" + "=" * 70)
    print("[Step 3] CALIBRATION — SPORTS ONLY")
    print("=" * 70)
    sports_buckets = run_calibration(sports, "Sports")

    # Brier scores
    print(f"\n  Brier Scores (sports only):")
    for strategy in ["market", "50pct", "fade_extremes"]:
        bs = brier_score(sports, strategy)
        print(f"    {strategy:20s}: {bs:.6f}")

    # PnL simulation
    print(f"\n  Simulated PnL (sports only, IN-SAMPLE):")
    print(f"  {'Thresh':>7} {'Trades':>7} {'WinRate':>8} "
          f"{'NetPnL':>10} {'Edge/Tr':>8} {'Sharpe':>7}")
    print(f"  {'-'*50}")
    pnl_results_sports = []
    for t in [0.01, 0.02, 0.03, 0.05, 0.07, 0.10]:
        r = simulate_pnl(sports, sports_buckets, t)
        pnl_results_sports.append(r)
        print(f"  {t:7.0%} {r['num_trades']:7d} {r['win_rate']:8.1%} "
              f"{r['net_pnl']:10.1f}c {r['avg_edge']:8.2f}c "
              f"{r['sharpe']:7.2f}")

    # ---- Step 4: Compare sports vs non-sports ----
    print("\n" + "=" * 70)
    print("[Step 4] COMPARISON: SPORTS vs NON-SPORTS")
    print("=" * 70)

    print("\n  --- Sports calibration ---")
    sports_buckets = run_calibration(sports, "Sports")

    print("\n  --- Non-sports calibration ---")
    nonsports_buckets = run_calibration(non_sports, "Non-sports")

    # Side by side comparison
    print(f"\n  Side-by-side:")
    print(f"  {'Bucket':>8} {'Sports':>18} {'Non-Sports':>18} {'Delta':>8}")
    print(f"  {'':>8} {'N':>6} {'Bias':>7} {'WR':>5} "
          f"{'N':>6} {'Bias':>7} {'WR':>5} {'':>8}")
    print(f"  {'-'*62}")
    for sb, nb in zip(sports_buckets, nonsports_buckets):
        s_bias = f"{sb['bias']:+.3f}" if sb['bias'] is not None else "  —  "
        n_bias = f"{nb['bias']:+.3f}" if nb['bias'] is not None else "  —  "
        s_wr = f"{sb['actual_win_rate']:.3f}" if sb['actual_win_rate'] is not None else "  —"
        n_wr = f"{nb['actual_win_rate']:.3f}" if nb['actual_win_rate'] is not None else "  —"
        delta = ""
        if sb['bias'] is not None and nb['bias'] is not None:
            d = sb['bias'] - nb['bias']
            delta = f"{d:+.3f}"
        print(f"  {sb['lo']:2d}-{sb['hi']:2d}c {sb['n']:6d} {s_bias} {s_wr} "
              f"{nb['n']:6d} {n_bias} {n_wr} {delta:>8}")

    # ---- Step 5: Per-sport breakdown ----
    print(f"\n  Per-sport strongest bias (buckets with n >= 30):")
    sport_breakdowns = {}
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        sport_markets = [m for m in sports if m["sport"] == sport]
        sb = bucket_analysis(sport_markets)
        sport_breakdowns[sport] = sb

        # Find strongest bias bucket with n >= 30
        valid = [b for b in sb if b["n"] >= 30 and b["bias"] is not None]
        if valid:
            best = max(valid, key=lambda b: abs(b["bias"]))
            print(f"    {sport:12s}: n={count:5d} | "
                  f"{best['lo']}-{best['hi']}c bias={best['bias']:+.3f} "
                  f"(n={best['n']})")
        else:
            print(f"    {sport:12s}: n={count:5d} | no bucket with n>=30")

    # ---- Generate outputs ----
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Calibration plots
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # Sports calibration
        plot_calibration(sports_buckets,
                         str(OUTPUT_DIR / "calibration_curve_sports.png"))

        # Comparison plot
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))

        for ax, buckets, title in [
            (ax1, sports_buckets, "Sports Only"),
            (ax2, nonsports_buckets, "Non-Sports (Crypto+Other)"),
        ]:
            valid = [b for b in buckets
                     if b["n"] > 0 and b["actual_win_rate"] is not None]
            x = [b["implied_prob"] for b in valid]
            y = [b["actual_win_rate"] for b in valid]
            yerr_lo = [b["actual_win_rate"] - b["ci_95_lo"] for b in valid]
            yerr_hi = [b["ci_95_hi"] - b["actual_win_rate"] for b in valid]

            ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="Perfect")
            ax.errorbar(x, y, yerr=[yerr_lo, yerr_hi], fmt="o", capsize=4,
                        color="steelblue", markersize=8)
            for b in valid:
                ax.annotate(f"n={b['n']}", (b["implied_prob"],
                            b["actual_win_rate"]),
                            textcoords="offset points", xytext=(8, -8),
                            fontsize=7, color="gray")
            ax.set_xlabel("Implied Probability")
            ax.set_ylabel("Actual Win Rate")
            ax.set_title(f"{title}\n(n={sum(b['n'] for b in buckets)})")
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.grid(True, alpha=0.3)
            ax.legend()

        fig.suptitle("Polymarket GLOBAL Calibration: Sports vs Non-Sports\n"
                     "(Data source: Gamma API — NOT Polymarket US)",
                     fontsize=13, fontweight="bold")
        fig.tight_layout()
        fig.savefig(str(OUTPUT_DIR / "calibration_comparison.png"), dpi=150)
        plt.close(fig)
        print(f"\n  Saved: {OUTPUT_DIR / 'calibration_comparison.png'}")
    except ImportError:
        print("  matplotlib not installed — skipping plots")

    # Report JSON
    report = {
        "data_caveat": (
            "Data is from Polymarket GLOBAL (Gamma API), NOT Polymarket US. "
            "Different platform, different markets, different pricing dynamics."
        ),
        "sports_count": len(sports),
        "non_sports_count": len(non_sports),
        "sport_counts": dict(sport_counts),
        "sports_calibration": sports_buckets,
        "non_sports_calibration": nonsports_buckets,
        "sports_brier": {s: brier_score(sports, s)
                         for s in ["market", "50pct", "fade_extremes"]},
        "sports_pnl": pnl_results_sports,
        "sport_breakdowns": sport_breakdowns,
    }
    report_path = OUTPUT_DIR / "phase1_sports_only_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Saved: {report_path}")

    # ---- Decision ----
    print("\n" + "=" * 70)
    print("DECISION GATE — SPORTS ONLY")
    print("=" * 70)

    sports_flagged = [b for b in sports_buckets
                      if b["bias"] is not None
                      and abs(b["bias"]) > 0.02 and b["n"] >= 30]
    positive_pnl = [r for r in pnl_results_sports if r["net_pnl"] > 0]

    if sports_flagged and positive_pnl:
        best = max(positive_pnl, key=lambda r: r["net_pnl"])
        print(f"  SIGNAL in sports: {len(sports_flagged)} biased buckets")
        print(f"  Best PnL: {best['net_pnl']:.0f}c at "
              f"{best['threshold']:.0%} ({best['num_trades']} trades)")
        print(f"  CAVEAT: Polymarket GLOBAL data, not US. In-sample only.")
        print(f"  NEXT: Need Polymarket US resolved data for real validation.")
    elif sports_flagged:
        print(f"  Bias exists in sports but doesn't survive fees.")
    else:
        print(f"  NO systematic bias in sports markets.")
        print(f"  Strategy A may not be viable for sports.")


if __name__ == "__main__":
    main()
