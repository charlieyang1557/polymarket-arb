#!/usr/bin/env python3
"""Experiments 1 + 3 — per-prefix edge persistence + mid-vs-settled bias.

Experiment 1 (edge persistence):
    For prefixes where the unconditional taker counterfactual was
    positive (aec-atp, asc-nhl, asc-mlb, aec-mlb, tsc-mlb), decompose
    by individual market_slug. Compute concentration ratio — what
    fraction of the total |P&L| contribution comes from the top 1-2
    markets? High concentration suggests sample luck; even spread
    suggests structural.

Experiment 3 (mid-vs-settled bias):
    For each prefix × side (yes_bid / no_bid), compute the average
    IMPLIED probability from fill prices and compare to the REALIZED
    settlement win rate for that side. If the bot's OBI fair-value is
    correct, implied ≈ realized. Significant gaps reveal a price-vs-
    settlement bias the bot was being tagged by.

Both experiments operate on the 326 historical fills + 110 resolved
outcomes. No new data needed.

Run:
    python scripts/research/edge_persistence_and_bias.py
    python scripts/research/edge_persistence_and_bias.py --filter-prefix aec-atp
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict

DB_PATH = "/Users/openclaw/polymarket-arb/data/poly_mm_live.db"
SETTLEMENT_CACHE = (
    "/Users/openclaw/polymarket-arb/.claude/worktrees/"
    "charming-mcclintock-caff3f/data/research/resolved_markets_cache.json"
)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def _prefix7(ticker: str) -> str:
    if not ticker or "-" not in ticker:
        return ticker or ""
    parts = ticker.split("-", 2)
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return ticker


def _taker_fee(price_cents: int, size: int) -> float:
    p = price_cents / 100
    return 0.02 * p * (1 - p) * 100 * size


def _flip_settled(fill: dict, outcome: str | None) -> float:
    if outcome is None:
        return 0.0
    price = fill["price"]
    size = fill["size"]
    if fill["side"] == "yes_bid":
        return ((price - 100) if outcome == "yes" else price) * size
    elif fill["side"] == "no_bid":
        return ((price - 100) if outcome == "no" else price) * size
    return 0.0


def per_market_taker_pnl(fills: list, outcomes: dict) -> dict:
    """Per-market taker counterfactual P&L. Returns
    dict[market_slug] -> {"settled_pnl_c": float, "fees_c": float,
                          "net_pnl_c": float, "n_fills": int, ...}.
    """
    per: dict = defaultdict(lambda: {
        "settled_pnl_c": 0.0, "fees_c": 0.0, "net_pnl_c": 0.0,
        "n_fills": 0, "n_contracts": 0, "n_settled": 0,
        "n_skipped": 0,
    })
    for f in fills:
        slug = f["ticker"]
        b = per[slug]
        b["n_fills"] += 1
        b["n_contracts"] += f["size"]
        outcome = outcomes.get(slug)
        if outcome is None:
            b["n_skipped"] += 1
            continue
        b["n_settled"] += 1
        s = _flip_settled(f, outcome)
        fee = _taker_fee(f["price"], f["size"])
        b["settled_pnl_c"] += s
        b["fees_c"] += fee
        b["net_pnl_c"] += (s - fee)
    return dict(per)


def concentration_ratio(market_pnls: list[float], top_n: int = 1) -> float:
    """Fraction of total |P&L| contribution from the top N markets by |P&L|.

    Returns 0 if list is empty. For a list with all-zero, returns 0.
    Higher value = more concentrated. Lower = more spread.
    """
    if not market_pnls:
        return 0.0
    abs_pnls = sorted((abs(p) for p in market_pnls), reverse=True)
    total = sum(abs_pnls)
    if total == 0:
        return 0.0
    top_sum = sum(abs_pnls[:top_n])
    return top_sum / total


def implied_vs_realized_per_prefix(fills: list, outcomes: dict) -> dict:
    """Per (prefix7, side), compute average implied probability vs realized
    settlement win rate. Returns
        dict[prefix7] -> dict[side] -> {
            avg_implied_prob, realized_win_rate, bias_c,
            n_settled, n_skipped
        }

    Bias is reported as cents-per-contract average:
      bias_c = (realized_win_rate - avg_implied_prob) * 100

    Positive bias_c = realized more often than priced = maker would gain.
    Negative bias_c = realized less than priced = maker would lose.
    """
    # Aggregate per (prefix, side)
    sums: dict = defaultdict(lambda: defaultdict(lambda: {
        "sum_implied_prob": 0.0,
        "n_total": 0,
        "n_settled": 0,
        "n_skipped": 0,
        "n_wins": 0,
        "n_contracts": 0,
    }))
    for f in fills:
        p = _prefix7(f["ticker"])
        side = f["side"]
        b = sums[p][side]
        b["n_total"] += 1
        b["n_contracts"] += f["size"]
        # Implied probability of side winning from price:
        # yes_bid at price P → implied prob YES = P/100
        # no_bid  at price P → implied prob NO  = P/100
        b["sum_implied_prob"] += f["price"] / 100
        outcome = outcomes.get(f["ticker"])
        if outcome is None:
            b["n_skipped"] += 1
            continue
        b["n_settled"] += 1
        if side == "yes_bid" and outcome == "yes":
            b["n_wins"] += 1
        elif side == "no_bid" and outcome == "no":
            b["n_wins"] += 1
    # Build result dict
    result: dict = {}
    for prefix, sides in sums.items():
        prefix_out: dict = {}
        for side, b in sides.items():
            if b["n_settled"] == 0:
                continue
            avg_impl = b["sum_implied_prob"] / b["n_total"]
            realized = b["n_wins"] / b["n_settled"]
            prefix_out[side] = {
                "avg_implied_prob": avg_impl,
                "realized_win_rate": realized,
                "bias_c": (realized - avg_impl) * 100,
                "n_settled": b["n_settled"],
                "n_skipped": b["n_skipped"],
                "n_contracts": b["n_contracts"],
            }
        if prefix_out:
            result[prefix] = prefix_out
    return result


# ---------------------------------------------------------------------------
# Data loading + reporting
# ---------------------------------------------------------------------------

def load_fills(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT id, session_id, ticker, side, price, size, fee, filled_at
        FROM mm_fills
        WHERE side IN ('yes_bid', 'no_bid')
        ORDER BY filled_at
    """).fetchall()
    return [{"id": r[0], "session_id": r[1], "ticker": r[2], "side": r[3],
             "price": r[4], "size": r[5], "fee": r[6], "filled_at": r[7]}
            for r in rows]


def load_outcomes(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        cache = json.load(f)
    outcomes = {}
    for slug, info in cache.items():
        if not isinstance(info, dict):
            continue
        for key in ("winning_outcome", "outcome", "resolution", "result"):
            v = info.get(key)
            if v:
                outcomes[slug] = "yes" if "yes" in str(v).lower() else "no"
                break
    return outcomes


def print_experiment_1(per_market: dict):
    """Edge persistence per prefix."""
    # Group per_market by prefix7
    by_prefix: dict = defaultdict(list)
    for slug, data in per_market.items():
        by_prefix[_prefix7(slug)].append((slug, data["net_pnl_c"], data["n_fills"]))

    print()
    print("=" * 90)
    print("EXPERIMENT 1 — Per-prefix edge persistence")
    print("=" * 90)
    print(f"{'prefix7':<14s} {'mkts':>5s} {'tot_net $':>10s} "
          f"{'top1 %':>8s} {'top2 %':>8s} {'verdict':>22s}")
    print("-" * 90)

    # Sort prefixes by aggregate magnitude (most interesting first)
    prefix_aggs = []
    for prefix, entries in by_prefix.items():
        total_pnl = sum(p for _, p, _ in entries)
        prefix_aggs.append((prefix, total_pnl, entries))
    prefix_aggs.sort(key=lambda x: -abs(x[1]))

    for prefix, total_pnl, entries in prefix_aggs:
        market_pnls = [p for _, p, _ in entries]
        n_markets = len(market_pnls)
        if n_markets < 1:
            continue
        c1 = concentration_ratio(market_pnls, top_n=1)
        c2 = concentration_ratio(market_pnls, top_n=2)
        if n_markets >= 5 and c1 < 0.30:
            verdict = "SPREAD (structural?)"
        elif n_markets >= 5 and c1 > 0.50:
            verdict = "CONCENTRATED (luck)"
        elif n_markets < 5:
            verdict = "SMALL SAMPLE"
        else:
            verdict = "INTERMEDIATE"
        print(f"{prefix:<14s} {n_markets:>5d} ${total_pnl/100:>+8.2f} "
              f"{c1*100:>7.1f}% {c2*100:>7.1f}% {verdict:>22s}")

    # Detail for top-3 by absolute aggregate
    print()
    print("Per-market detail (top 3 prefixes by |aggregate net|):")
    for prefix, total_pnl, entries in prefix_aggs[:3]:
        print(f"\n  {prefix} (total ${total_pnl/100:+.2f}, n={len(entries)} markets):")
        entries_sorted = sorted(entries, key=lambda e: -abs(e[1]))
        for slug, pnl, n in entries_sorted[:8]:
            print(f"    {slug:<48s} fills={n:>2d}  net=${pnl/100:>+7.2f}")
        if len(entries_sorted) > 8:
            rest_sum = sum(e[1] for e in entries_sorted[8:]) / 100
            print(f"    ... +{len(entries_sorted) - 8} more markets (sum ${rest_sum:+.2f})")


def print_experiment_3(bias_per_prefix: dict):
    """Mid-vs-settled bias check."""
    print()
    print("=" * 90)
    print("EXPERIMENT 3 — Mid-vs-settled bias check (per prefix7 × side)")
    print("=" * 90)
    print(f"{'prefix7':<14s} {'side':<8s} {'n_set':>6s} {'contracts':>10s} "
          f"{'implied %':>10s} {'realized %':>11s} {'bias c':>8s}  {'verdict':<25s}")
    print("-" * 90)

    rows = []
    for prefix, sides in bias_per_prefix.items():
        for side in ("yes_bid", "no_bid"):
            if side in sides:
                rows.append((prefix, side, sides[side]))

    # Sort by total contracts (most data first)
    rows.sort(key=lambda r: -r[2]["n_contracts"])

    for prefix, side, b in rows:
        bias = b["bias_c"]
        if abs(bias) < 5:
            verdict = "near fair"
        elif bias < -5:
            verdict = "OVERPRICED (maker loses)"
        else:
            verdict = "UNDERPRICED (maker gains)"
        print(f"{prefix:<14s} {side:<8s} {b['n_settled']:>6d} {b['n_contracts']:>10d} "
              f"{b['avg_implied_prob']*100:>9.1f}% {b['realized_win_rate']*100:>10.1f}% "
              f"{bias:>+7.1f}c  {verdict:<25s}")

    # Cross-side aggregate bias: maker loses when (yes_bias < 0) AND (no_bias > 0)
    print()
    print("Cross-side summary per prefix (positive bias = side underpriced = maker gains):")
    print(f"{'prefix7':<14s} {'yes_bias':>10s} {'no_bias':>10s}  {'interpretation':<40s}")
    print("-" * 80)
    for prefix in sorted(bias_per_prefix.keys()):
        yes = bias_per_prefix[prefix].get("yes_bid", {}).get("bias_c")
        no_ = bias_per_prefix[prefix].get("no_bid", {}).get("bias_c")
        if yes is None and no_ is None:
            continue
        yes_str = f"{yes:>+6.1f}c" if yes is not None else "    -"
        no_str = f"{no_:>+6.1f}c" if no_ is not None else "    -"
        # Interpret pattern
        if yes is not None and no_ is not None:
            if yes < -5 and no_ < -5:
                interp = "Both overpriced — OBI biased too high"
            elif yes > 5 and no_ > 5:
                interp = "Both underpriced — OBI biased too low"
            elif yes < -5 and no_ > 5:
                interp = "YES overpriced, NO underpriced (BUY-NO edge)"
            elif yes > 5 and no_ < -5:
                interp = "YES underpriced, NO overpriced (BUY-YES edge)"
            else:
                interp = "Near fair"
        else:
            interp = "Insufficient data"
        print(f"{prefix:<14s} {yes_str:>10s} {no_str:>10s}  {interp:<40s}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--filter-prefix", default=None,
                        help="Restrict to fills whose ticker has this prefix7")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    fills = load_fills(conn)
    conn.close()
    print(f"Loaded {len(fills)} fills from {args.db}")

    if args.filter_prefix:
        fills = [f for f in fills if _prefix7(f["ticker"]) == args.filter_prefix]
        print(f"Filtered to {len(fills)} fills matching prefix7={args.filter_prefix}")

    outcomes = load_outcomes(SETTLEMENT_CACHE)
    print(f"Loaded {len(outcomes)} resolved outcomes")

    # Experiment 1
    per_market = per_market_taker_pnl(fills, outcomes)
    print_experiment_1(per_market)

    # Experiment 3
    bias_per_prefix = implied_vs_realized_per_prefix(fills, outcomes)
    print_experiment_3(bias_per_prefix)


if __name__ == "__main__":
    main()
