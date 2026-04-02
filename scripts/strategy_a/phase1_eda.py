#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy A Phase 1: Exploratory Data Analysis — Calibration & Bias Detection.

Uses 135K resolved Polymarket markets (Gamma API data) to determine if
systematic pricing bias exists that survives transaction costs.

Data source: data/historical/raw_markets.json (Gamma API, fetched 2026-03-11)
Key fields:
  - outcomePrices: '["1","0"]' (YES won) or '["0","1"]' (NO won)
  - lastTradePrice: float 0-1 (price of YES outcome before settlement)
  - volumeNum: total volume in dollars
  - slug: market identifier (parsed for sport detection)
  - feeType: 'crypto_fees' vs others (category proxy)

IMPORTANT: Step 4 PnL simulation uses in-sample bucket rates for in-sample
bets. This WILL be overfitted. Phase 2 will add train/test split.
Phase 1 is exploratory only — checking if any signal exists at all.

Usage:
    python scripts/strategy_a/phase1_eda.py
    python scripts/strategy_a/phase1_eda.py --data data/historical/raw_markets.json
    python scripts/strategy_a/phase1_eda.py --min-volume 100
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

OUTPUT_DIR = Path("data/strategy_a")

# Polymarket taker fee formula (same as Kalshi: ceil(0.0175 * p * (1-p) * 100))
TAKER_FEE_COEFF = 0.0175
SLIPPAGE_CENTS = 1  # conservative 1c slippage on taker orders


# ---------------------------------------------------------------------------
# Step 0: Data loading
# ---------------------------------------------------------------------------

def load_resolved_markets(path: str, min_volume: float = 0) -> list[dict]:
    """Load and parse resolved markets from Gamma API data.

    Returns list of dicts with normalized fields:
      slug, question, price (0-100 cents), outcome (1=YES, 0=NO),
      volume, fee_type, sport (parsed from slug)
    """
    with open(path) as f:
        raw = json.load(f)

    markets = []
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

        # Only clean 0/1 settlements
        if p0 == 1 and p1 == 0:
            outcome = 1  # YES won
        elif p0 == 0 and p1 == 1:
            outcome = 0  # NO won
        else:
            continue  # void, push, or unresolved

        # Need a price signal
        ltp = m.get("lastTradePrice")
        if ltp is None or ltp == 0:
            continue
        try:
            price_frac = float(ltp)
        except (ValueError, TypeError):
            continue
        if price_frac <= 0 or price_frac >= 1:
            continue  # edge prices not useful

        price_cents = round(price_frac * 100)

        # Volume filter
        vol = float(m.get("volumeNum", 0) or 0)
        if vol < min_volume:
            continue

        # Parse sport from slug
        sport = parse_sport(m.get("slug", ""), m.get("question", ""),
                            m.get("feeType", ""))

        markets.append({
            "slug": m.get("slug", ""),
            "question": (m.get("question") or "")[:80],
            "price_cents": price_cents,
            "outcome": outcome,
            "volume": vol,
            "fee_type": m.get("feeType", ""),
            "sport": sport,
        })

    return markets


def parse_sport(slug: str, question: str, fee_type: str) -> str:
    """Best-effort sport/category detection from slug and question."""
    slug_lower = slug.lower()
    q_lower = question.lower()
    text = slug_lower + " " + q_lower

    if "updown" in slug_lower or fee_type == "crypto_fees":
        return "crypto"

    sport_keywords = {
        "nba": "nba", "nfl": "nfl", "mlb": "mlb", "nhl": "nhl",
        "ufc": "ufc", "mma": "ufc",
        "ncaa": "ncaa", "cbb": "ncaa", "cfb": "ncaa",
        "atp": "tennis", "wta": "tennis", "tennis": "tennis",
        "epl": "soccer", "ucl": "soccer", "mls": "soccer",
        "soccer": "soccer", "football": "soccer",
        "f1": "motorsport", "nascar": "motorsport",
        "boxing": "boxing",
        "cricket": "cricket",
    }
    for keyword, sport in sport_keywords.items():
        if keyword in text:
            return sport

    if "president" in text or "election" in text or "trump" in text:
        return "politics"

    return "other"


# ---------------------------------------------------------------------------
# Step 2: Calibration curve
# ---------------------------------------------------------------------------

def bucket_analysis(markets: list[dict], bucket_size: int = 10) -> list[dict]:
    """Bucket markets by price, compute actual win rate vs implied prob.

    Returns list of bucket dicts with: lo, hi, n, implied_prob,
    actual_win_rate, bias, ci_95_lo, ci_95_hi.
    """
    buckets = []
    for lo in range(0, 100, bucket_size):
        hi = lo + bucket_size
        in_bucket = [m for m in markets
                     if lo < m["price_cents"] <= hi]  # (lo, hi]
        # Handle 0-10 bucket specially to include price=1
        if lo == 0:
            in_bucket = [m for m in markets
                         if 0 < m["price_cents"] <= hi]

        n = len(in_bucket)
        if n == 0:
            buckets.append({
                "lo": lo, "hi": hi, "n": 0,
                "implied_prob": (lo + hi) / 200,
                "actual_win_rate": None, "bias": None,
                "ci_95_lo": None, "ci_95_hi": None,
            })
            continue

        implied = sum(m["price_cents"] for m in in_bucket) / n / 100
        wins = sum(m["outcome"] for m in in_bucket)
        actual = wins / n
        bias = actual - implied

        # 95% CI using normal approximation
        se = math.sqrt(actual * (1 - actual) / n) if n > 1 else 0
        ci_lo = actual - 1.96 * se
        ci_hi = actual + 1.96 * se

        buckets.append({
            "lo": lo, "hi": hi, "n": n,
            "implied_prob": round(implied, 4),
            "actual_win_rate": round(actual, 4),
            "bias": round(bias, 4),
            "ci_95_lo": round(ci_lo, 4),
            "ci_95_hi": round(ci_hi, 4),
        })

    return buckets


# ---------------------------------------------------------------------------
# Step 3: Brier Scores
# ---------------------------------------------------------------------------

def brier_score(markets: list[dict], strategy: str = "market") -> float:
    """Compute Brier Score for a prediction strategy.

    Strategies:
      "market" — use market price as probability
      "50pct" — always predict 50%
      "fade_extremes" — shrink toward 50% (if >70c predict 65c, etc.)
    """
    if not markets:
        return 1.0

    total = 0.0
    for m in markets:
        outcome = m["outcome"]
        p = m["price_cents"] / 100

        if strategy == "market":
            pred = p
        elif strategy == "50pct":
            pred = 0.5
        elif strategy == "fade_extremes":
            if p > 0.70:
                pred = max(0.65, p - 0.05)
            elif p < 0.30:
                pred = min(0.35, p + 0.05)
            else:
                pred = p
        else:
            pred = p

        total += (pred - outcome) ** 2

    return round(total / len(markets), 6)


# ---------------------------------------------------------------------------
# Step 4: Simulated PnL
# ---------------------------------------------------------------------------

def taker_fee_cents(price_cents: int) -> float:
    """Taker fee in cents for a trade at given price."""
    p = price_cents / 100
    return math.ceil(TAKER_FEE_COEFF * p * (1 - p) * 100)


def simulate_pnl(markets: list[dict], buckets: list[dict],
                 threshold: float) -> dict:
    """Simulate directional taker strategy using bucket bias.

    For each market: if bucket bias > threshold, bet on the biased side.
    Uses IN-SAMPLE bucket rates (overfitted — Phase 2 adds OOS).

    Returns dict with: threshold, num_trades, win_rate, gross_pnl,
    fees, net_pnl, avg_edge, sharpe.
    """
    # Build bucket lookup: price_cents -> bucket bias and actual rate
    bucket_lookup = {}
    for b in buckets:
        if b["n"] == 0 or b["bias"] is None:
            continue
        for p in range(b["lo"] + 1, b["hi"] + 1):
            bucket_lookup[p] = {
                "bias": b["bias"],
                "actual": b["actual_win_rate"],
                "implied": b["implied_prob"],
            }

    trades = []
    for m in markets:
        info = bucket_lookup.get(m["price_cents"])
        if info is None:
            continue

        bias = info["bias"]
        if abs(bias) < threshold:
            continue

        price = m["price_cents"]
        outcome = m["outcome"]
        fee = taker_fee_cents(price)

        if bias > 0:
            # Actual > implied → YES underpriced → buy YES
            buy_price = price + SLIPPAGE_CENTS
            won = (outcome == 1)
            pnl = (100 - buy_price) if won else -buy_price
        else:
            # Actual < implied → NO underpriced → buy NO
            buy_price = (100 - price) + SLIPPAGE_CENTS
            won = (outcome == 0)
            pnl = (100 - buy_price) if won else -buy_price

        pnl -= fee
        trades.append({"pnl": pnl, "won": won})

    if not trades:
        return {
            "threshold": threshold, "num_trades": 0, "win_rate": 0,
            "gross_pnl": 0, "fees": 0, "net_pnl": 0,
            "avg_edge": 0, "sharpe": 0,
        }

    num = len(trades)
    wins = sum(1 for t in trades if t["won"])
    pnls = [t["pnl"] for t in trades]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / num
    std_pnl = math.sqrt(sum((p - avg_pnl) ** 2 for p in pnls) / num) if num > 1 else 1
    sharpe = (avg_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0

    total_fees = sum(taker_fee_cents(m["price_cents"]) for m in markets
                     if bucket_lookup.get(m["price_cents"])
                     and abs(bucket_lookup[m["price_cents"]]["bias"]) >= threshold)

    return {
        "threshold": threshold,
        "num_trades": num,
        "win_rate": round(wins / num, 4),
        "gross_pnl": round(total_pnl + total_fees, 1),
        "fees": round(total_fees, 1),
        "net_pnl": round(total_pnl, 1),
        "avg_edge": round(avg_pnl, 2),
        "sharpe": round(sharpe, 2),
    }


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def plot_calibration(buckets: list[dict], output_path: str):
    """Generate calibration curve PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not installed — skipping plot")
        return

    valid = [b for b in buckets if b["n"] > 0 and b["actual_win_rate"] is not None]
    x = [b["implied_prob"] for b in valid]
    y = [b["actual_win_rate"] for b in valid]
    yerr_lo = [b["actual_win_rate"] - b["ci_95_lo"] for b in valid]
    yerr_hi = [b["ci_95_hi"] - b["actual_win_rate"] for b in valid]
    sizes = [max(20, min(200, b["n"] / 50)) for b in valid]

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="Perfect calibration")
    ax.errorbar(x, y, yerr=[yerr_lo, yerr_hi], fmt="o", capsize=4,
                color="steelblue", markersize=8, label="Actual win rate")

    # Annotate buckets with n
    for b in valid:
        ax.annotate(f"n={b['n']}", (b["implied_prob"], b["actual_win_rate"]),
                    textcoords="offset points", xytext=(8, -8), fontsize=7,
                    color="gray")

    ax.set_xlabel("Implied Probability (Market Price)", fontsize=12)
    ax.set_ylabel("Actual Win Rate", fontsize=12)
    ax.set_title("Polymarket Calibration Curve\n(135K resolved markets, Gamma API)",
                 fontsize=14)
    ax.legend(fontsize=10)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"  Saved: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Strategy A Phase 1: Calibration & Bias EDA")
    parser.add_argument("--data", default="data/historical/raw_markets.json",
                        help="Path to raw markets JSON")
    parser.add_argument("--min-volume", type=float, default=0,
                        help="Minimum volume filter (dollars)")
    args = parser.parse_args()

    print("=" * 70)
    print("STRATEGY A — PHASE 1: EXPLORATORY DATA ANALYSIS")
    print("=" * 70)

    # ---- Step 0: Load data ----
    print("\n[Step 0] Loading resolved markets...")
    if not os.path.exists(args.data):
        print(f"  FATAL: {args.data} not found")
        print("  Run: python scripts/fetch_historical.py to fetch data first")
        sys.exit(1)

    markets = load_resolved_markets(args.data, min_volume=args.min_volume)
    print(f"  Loaded: {len(markets)} resolved markets with valid price")

    if len(markets) < 100:
        print("  WARNING: Too few markets for meaningful analysis")

    # ---- Step 1: Data cleaning summary ----
    print("\n[Step 1] Data summary")
    yes_won = sum(1 for m in markets if m["outcome"] == 1)
    no_won = sum(1 for m in markets if m["outcome"] == 0)
    print(f"  YES won: {yes_won} ({100*yes_won/len(markets):.1f}%)")
    print(f"  NO won:  {no_won} ({100*no_won/len(markets):.1f}%)")

    # By sport/category
    sport_counts = defaultdict(int)
    for m in markets:
        sport_counts[m["sport"]] += 1
    print(f"\n  By category:")
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        print(f"    {sport:12s}: {count:6d} ({100*count/len(markets):5.1f}%)")

    # Sample markets
    print(f"\n  Sample markets:")
    for m in markets[:3]:
        print(f"    {m['slug'][:40]:40s} p={m['price_cents']:2d}c "
              f"{'YES' if m['outcome'] else 'NO':3s} vol=${m['volume']:.0f}")

    # ---- Step 2: Calibration curve ----
    print("\n[Step 2] Calibration analysis (10c buckets)")
    buckets = bucket_analysis(markets)

    print(f"\n  {'Bucket':>8} {'N':>7} {'Implied':>8} {'Actual':>8} "
          f"{'Bias':>7} {'95% CI':>16} {'Signal':>8}")
    print(f"  {'-'*62}")

    flagged = []
    for b in buckets:
        if b["n"] == 0:
            print(f"  {b['lo']:2d}-{b['hi']:2d}c {'—':>7}")
            continue
        signal = ""
        if abs(b["bias"]) > 0.02 and b["n"] >= 30:
            signal = " ***"
            flagged.append(b)
        ci_str = f"[{b['ci_95_lo']:.3f}, {b['ci_95_hi']:.3f}]"
        print(f"  {b['lo']:2d}-{b['hi']:2d}c {b['n']:7d} {b['implied_prob']:8.3f} "
              f"{b['actual_win_rate']:8.3f} {b['bias']:+7.3f} {ci_str:>16}{signal}")

    if flagged:
        print(f"\n  *** {len(flagged)} buckets with |bias| > 2% AND n >= 30:")
        for b in flagged:
            direction = "YES underpriced" if b["bias"] > 0 else "NO underpriced"
            print(f"      {b['lo']}-{b['hi']}c: {b['bias']:+.3f} ({direction})")
    else:
        print("\n  No buckets with statistically notable bias (|bias| > 2%, n >= 30)")

    # ---- Step 3: Brier Scores ----
    print("\n[Step 3] Brier Score comparison (lower = better)")
    strategies = ["market", "50pct", "fade_extremes"]
    brier_results = {}
    for s in strategies:
        bs = brier_score(markets, s)
        brier_results[s] = bs
        print(f"  {s:20s}: {bs:.6f}")

    best = min(brier_results, key=brier_results.get)
    print(f"  Best: {best}")

    # ---- Step 4: Simulated PnL ----
    print("\n[Step 4] Simulated PnL (IN-SAMPLE — OVERFITTED, see note)")
    print(f"  Fee: ceil({TAKER_FEE_COEFF} * p * (1-p) * 100) per trade")
    print(f"  Slippage: {SLIPPAGE_CENTS}c per trade")
    print()

    thresholds = [0.01, 0.02, 0.03, 0.05, 0.07, 0.10]
    pnl_results = []

    print(f"  {'Thresh':>7} {'Trades':>7} {'WinRate':>8} {'GrossPnL':>10} "
          f"{'Fees':>8} {'NetPnL':>10} {'Edge/Tr':>8} {'Sharpe':>7}")
    print(f"  {'-'*67}")

    for t in thresholds:
        result = simulate_pnl(markets, buckets, t)
        pnl_results.append(result)
        print(f"  {t:7.0%} {result['num_trades']:7d} {result['win_rate']:8.1%} "
              f"{result['gross_pnl']:10.1f}c {result['fees']:8.1f}c "
              f"{result['net_pnl']:10.1f}c {result['avg_edge']:8.2f}c "
              f"{result['sharpe']:7.2f}")

    print("\n  WARNING: This is in-sample. Real edge will be lower.")
    print("  Phase 2 will add train/test split for out-of-sample validation.")

    # ---- Step 5: Sport breakdown ----
    print("\n[Step 5] Sport/category breakdown")
    sport_buckets = {}
    for sport, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        if count < 30:
            print(f"  {sport}: n={count} (too small, skipping)")
            continue
        sport_markets = [m for m in markets if m["sport"] == sport]
        sb = bucket_analysis(sport_markets)
        sport_buckets[sport] = sb

        # Find strongest bias in this sport
        best_bias = max(sb, key=lambda b: abs(b["bias"] or 0))
        if best_bias["bias"] is not None and best_bias["n"] >= 30:
            print(f"  {sport:12s}: n={count:6d} | strongest bias: "
                  f"{best_bias['lo']}-{best_bias['hi']}c "
                  f"bias={best_bias['bias']:+.3f} (n={best_bias['n']})")
        else:
            print(f"  {sport:12s}: n={count:6d} | no bucket with n>=30")

    # ---- Generate outputs ----
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Calibration plot
    plot_calibration(buckets, str(OUTPUT_DIR / "calibration_curve.png"))

    # Report JSON
    report = {
        "data_source": args.data,
        "total_markets": len(markets),
        "min_volume_filter": args.min_volume,
        "yes_won": yes_won,
        "no_won": no_won,
        "sport_counts": dict(sport_counts),
        "calibration_buckets": buckets,
        "brier_scores": brier_results,
        "pnl_simulations": pnl_results,
        "sport_breakdown": {
            sport: sb for sport, sb in sport_buckets.items()
        },
        "flagged_buckets": [
            {"range": f"{b['lo']}-{b['hi']}c", "bias": b["bias"], "n": b["n"]}
            for b in flagged
        ],
    }
    report_path = OUTPUT_DIR / "phase1_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Saved: {report_path}")

    # ---- Decision gate ----
    print("\n" + "=" * 70)
    print("PHASE 1 DECISION GATE")
    print("=" * 70)

    has_bias = len(flagged) > 0
    # Check if any threshold shows positive net PnL
    positive_pnl = [r for r in pnl_results if r["net_pnl"] > 0]

    if has_bias and positive_pnl:
        best_pnl = max(positive_pnl, key=lambda r: r["net_pnl"])
        print(f"  SIGNAL DETECTED: {len(flagged)} biased buckets, "
              f"best PnL at {best_pnl['threshold']:.0%} threshold = "
              f"{best_pnl['net_pnl']:.0f}c ({best_pnl['num_trades']} trades)")
        print(f"  RECOMMENDATION: Proceed to Phase 2 (backtest with OOS validation)")
        print(f"  CAVEAT: This is in-sample. Expect 50-70% shrinkage out-of-sample.")
    elif has_bias:
        print(f"  BIAS EXISTS but doesn't survive fees at any threshold.")
        print(f"  RECOMMENDATION: Investigate volume/sport filters to isolate signal.")
    else:
        print(f"  NO SYSTEMATIC BIAS detected (|bias| > 2% with n >= 30).")
        print(f"  RECOMMENDATION: Strategy A may not be viable. Consider Strategy C.")


if __name__ == "__main__":
    main()
