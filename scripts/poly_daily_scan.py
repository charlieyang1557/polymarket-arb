#!/usr/bin/env python3
"""
Polymarket US Daily MM Target Scanner.

Scans active sports markets on Polymarket US and ranks them for
market making. Uses polymarket-us SDK (public endpoints, no auth).

Usage:
    python scripts/poly_daily_scan.py
    python scripts/poly_daily_scan.py --max-markets 5 --max-check 100
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.poly_client import PolyClient, normalize_orderbook, calculate_maker_fee

OUTPUT_DIR = Path("data/polymarket_diagnostic")

# Rebate config (sports default for Polymarket US)
TAKER_FEE_PCT = 0.02
REBATE_PCT = 0.25


# ---------------------------------------------------------------------------
# Pure helpers (tested)
# ---------------------------------------------------------------------------

def poly_net_spread_cents(spread_cents: int, midpoint_cents: float) -> float:
    """Net spread including maker rebate income.

    Polymarket makers GET PAID: rebate = 25% of taker fee per side.
    net_spread = gross_spread + 2 * rebate_per_side

    Compare to Kalshi: net_spread = gross_spread - 2 * maker_fee_per_side
    """
    if spread_cents == 0:
        return 0

    p = midpoint_cents / 100
    taker_fee_per_side = TAKER_FEE_PCT * p * (1 - p) * 100
    rebate_per_side = REBATE_PCT * taker_fee_per_side

    return round(spread_cents + 2 * rebate_per_side, 2)


def apply_prefilters(c: dict) -> bool:
    """Apply binary pre-filters to a candidate.

    Filters (adapted from Kalshi, relaxed for Polymarket rebates):
      - spread >= 1c and <= 10c (1c is profitable with maker rebates)
      - midpoint 20-80c (avoid extremes)
      - net_spread > 0 (profitable after rebate — always true on Poly)
      - symmetry 0.2-5.0
      - Both sides have depth > 0
    """
    return (c.get("spread", 0) >= 1
            and c.get("spread", 0) <= 10
            and 20 <= c.get("midpoint", 0) <= 80
            and c.get("net_spread", 0) > 0
            and c.get("best_yes_depth", 0) > 0
            and c.get("best_no_depth", 0) > 0
            and 0.2 <= c.get("symmetry", 0) <= 5.0)


def avg_rank(values: list, ascending: bool = True) -> list[float]:
    """Return average ranks. ascending=True means lowest value = rank 1."""
    indexed = sorted(enumerate(values),
                     key=lambda x: x[1] if ascending else -x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_r = sum(range(i + 1, j + 1)) / (j - i)
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_r
        i = j
    return ranks


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Rank-based composite scoring.

    Two metrics (no trades_per_hour on Polymarket yet):
      1. net_spread (descending — higher = better)
      2. binding_queue (ascending — lower = better)

    composite = average of two ranks. Lower = better.
    """
    passing = [c for c in candidates if c.get("passes")]
    failing = [c for c in candidates if not c.get("passes")]

    if not passing:
        return failing + passing

    net_spreads = [c["net_spread"] for c in passing]
    queues = [c["binding_queue"] for c in passing]

    spread_ranks = avg_rank(net_spreads, ascending=False)
    queue_ranks = avg_rank(queues, ascending=True)

    for i, c in enumerate(passing):
        c["rank_spread"] = spread_ranks[i]
        c["rank_queue"] = queue_ranks[i]
        c["composite_rank"] = round(
            (spread_ranks[i] + queue_ranks[i]) / 2, 2)

    passing.sort(key=lambda c: c["composite_rank"])
    return passing + failing


# ---------------------------------------------------------------------------
# Main (SDK-dependent)
# ---------------------------------------------------------------------------

def scan_active_markets(client: PolyClient) -> list[dict]:
    """Fetch all active open markets and extract BBO data."""
    print("  Fetching active markets...")
    all_markets = []
    offset = 0
    page_size = 100

    while True:
        resp = client.client.markets.list({
            "limit": page_size,
            "offset": offset,
            "active": True,
            "closed": False,
            "orderBy": ["volume"],
            "orderDirection": "desc",
        })
        batch = resp.get("markets", [])
        if not batch:
            break
        all_markets.extend(batch)
        offset += page_size
        if len(batch) < page_size:
            break
        time.sleep(0.1)

    print(f"  Found {len(all_markets)} active open markets")

    # Enrich with BBO
    candidates = []
    for i, m in enumerate(all_markets):
        slug = m.get("slug", "")
        if not slug:
            continue

        try:
            bbo = client.get_bbo(slug)
        except Exception:
            continue

        best_bid = bbo.get("best_bid_cents", 0)
        best_ask = bbo.get("best_ask_cents", 0)
        shares = bbo.get("shares_traded", 0)

        if best_bid <= 0 or best_ask <= 0:
            continue

        spread = best_ask - best_bid
        midpoint = (best_bid + best_ask) / 2

        candidates.append({
            "slug": slug,
            "question": (m.get("question") or "")[:70],
            "market_type": m.get("marketType", ""),
            "series_slug": m.get("seriesSlug", ""),
            "category": m.get("category", "sports"),
            "spread": spread,
            "midpoint": midpoint,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "shares_traded": shares,
            "game_start_time": m.get("gameStartTime", ""),
            "end_date": m.get("endDate", ""),
        })

        if (i + 1) % 100 == 0:
            print(f"    ... {i + 1}/{len(all_markets)} ({len(candidates)} with books)")

        time.sleep(0.05)

    print(f"  {len(candidates)} markets with live BBO data")
    return candidates


def deep_check(client: PolyClient, candidates: list[dict],
               max_check: int = 50) -> list[dict]:
    """Fetch full orderbooks, compute depth/symmetry, apply filters."""
    # Pre-sort: prioritize markets with actual trading activity
    # Key insight: untraded future markets show 94c spread (bid=3, ask=97)
    # Real MM targets are today's games with 1-5c spreads and shares > 0
    candidates.sort(key=lambda c: (
        c["shares_traded"] > 0,   # traded markets first
        -abs(c["midpoint"] - 50), # midpoint near 50c preferred
        -c["spread"],             # then by spread (within traded markets)
    ), reverse=True)
    to_check = [c for c in candidates
                if c["spread"] >= 1 and c["midpoint"] >= 20
                and c["midpoint"] <= 80][:max_check]

    print(f"  Deep-checking {len(to_check)} markets (spread >= 2c)...")

    checked = []
    for i, c in enumerate(to_check):
        slug = c["slug"]
        try:
            book = client.get_orderbook(slug)
            fp = book.get("orderbook_fp", {})
            yes_raw = fp.get("yes_dollars", [])
            no_raw = fp.get("no_dollars", [])

            # Parse to [price_cents, quantity] pairs
            yes_bids = [[round(float(p) * 100), int(float(q))]
                        for p, q in yes_raw]
            no_bids = [[round(float(p) * 100), int(float(q))]
                       for p, q in no_raw]

            yes_depth = sum(q for _, q in yes_bids)
            no_depth = sum(q for _, q in no_bids)
            best_yes = yes_bids[-1][1] if yes_bids else 0
            best_no = no_bids[-1][1] if no_bids else 0

            if yes_depth > 0 and no_depth > 0:
                sym = yes_depth / no_depth
            elif yes_depth > 0:
                sym = 999.0
            elif no_depth > 0:
                sym = 0.001
            else:
                sym = 0.0

            c["yes_depth"] = yes_depth
            c["no_depth"] = no_depth
            c["best_yes_depth"] = best_yes
            c["best_no_depth"] = best_no
            c["symmetry"] = round(sym, 3)
            c["binding_queue"] = max(yes_depth, no_depth)

            # Net spread with rebate
            c["net_spread"] = poly_net_spread_cents(c["spread"], c["midpoint"])

            # Apply pre-filters
            c["passes"] = apply_prefilters(c)

        except Exception as e:
            c["yes_depth"] = 0
            c["no_depth"] = 0
            c["best_yes_depth"] = 0
            c["best_no_depth"] = 0
            c["symmetry"] = 0.0
            c["binding_queue"] = 0
            c["net_spread"] = 0
            c["passes"] = False
            c["error"] = str(e)

        checked.append(c)

        if (i + 1) % 25 == 0:
            print(f"    ... {i + 1}/{len(to_check)}")

        time.sleep(0.05)

    return checked


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket US daily MM target scanner")
    parser.add_argument("--max-markets", type=int, default=5,
                        help="Max targets to select (default: 5)")
    parser.add_argument("--max-check", type=int, default=100,
                        help="Max markets to deep-check (default: 100)")
    args = parser.parse_args()

    client = PolyClient()  # public only

    print("=" * 70)
    print("POLYMARKET US — DAILY MM SCANNER")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Phase 1: Fetch all active markets + BBO
    candidates = scan_active_markets(client)

    if not candidates:
        print("\n  No active markets with live books.")
        return

    # Quick stats
    spreads = [c["spread"] for c in candidates]
    print(f"\n  Spread distribution:")
    for lo, hi, label in [(0, 2, "0-1c"), (2, 5, "2-4c"), (5, 10, "5-9c"),
                           (10, 20, "10-19c"), (20, 100, "20c+")]:
        count = sum(1 for s in spreads if lo <= s < hi)
        print(f"    {label:>6}: {count}")

    # Phase 2: Deep check (orderbook + filters)
    checked = deep_check(client, candidates, max_check=args.max_check)

    # Phase 3: Rank passing candidates
    ranked = rank_candidates(checked)

    passing = [c for c in ranked if c.get("passes")]
    print(f"\n  Passing filters: {len(passing)} / {len(checked)} checked")
    print(f"  Filters: spread 2-10c, mid 20-80c, sym 0.2-5.0, both sides depth>0")

    # Table
    print()
    header = (f"{'#':>2} {'Pass':>4} {'Series':<12} {'Type':<10} "
              f"{'Sprd':>4} {'Net':>5} {'Mid':>4} {'Sym':>5} "
              f"{'L1Q':>6} {'TotQ':>6} {'Rank':>5} {'Question':<35}")
    print(header)
    print("-" * len(header))

    for i, c in enumerate(ranked, 1):
        flag = " OK " if c.get("passes") else "FAIL"
        series = (c.get("series_slug") or "")[:11]
        mt = (c.get("market_type") or "")[:9]
        sym = c.get("symmetry", 0)
        sym_s = f"{sym:.2f}" if sym < 100 else ">100"
        net = c.get("net_spread", 0)
        best_depth = max(c.get("best_yes_depth", 0), c.get("best_no_depth", 0))
        totq = c.get("binding_queue", 0)
        rank_s = f"{c['composite_rank']:.1f}" if "composite_rank" in c else "-"
        print(f"{i:2d} {flag} {series:<12} {mt:<10} "
              f"{c['spread']:4d} {net:5.1f} {c['midpoint']:4.0f} {sym_s:>5} "
              f"{best_depth:6d} {totq:6d} {rank_s:>5} "
              f"{c.get('question', '')[:35]}")

    # Rank detail for passing
    if passing:
        print(f"\n  Rank detail (passing):")
        for c in passing:
            print(f"    {c['slug']:<45} "
                  f"rk_sprd={c['rank_spread']:.1f} "
                  f"rk_queue={c['rank_queue']:.1f} → "
                  f"composite={c['composite_rank']:.2f}")

    # Phase 4: Select targets
    targets = passing[:args.max_markets]

    if targets:
        print(f"\n  Selected targets ({len(targets)}):")
        for t in targets:
            print(f"    {t['slug']:<45} spread={t['spread']}c "
                  f"net={t['net_spread']:.1f}c sym={t['symmetry']:.2f} "
                  f"queue={t['binding_queue']}")
    else:
        print("\n  No markets pass all filters.")

    # Comparison vs Kalshi
    if passing:
        avg_net = sum(c["net_spread"] for c in passing) / len(passing)
        avg_spread = sum(c["spread"] for c in passing) / len(passing)
        print(f"\n  Polymarket vs Kalshi comparison:")
        print(f"    Avg gross spread: {avg_spread:.1f}c "
              f"(Kalshi was 2-3c)")
        print(f"    Avg net spread:   {avg_net:.1f}c "
              f"(Kalshi was 0-1c)")
        print(f"    Fee structure:    Makers get PAID {REBATE_PCT*100:.0f}% rebate "
              f"(Kalshi charges makers)")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_file = OUTPUT_DIR / f"daily_scan_{date_str}.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_active": len(candidates),
            "total_checked": len(checked),
            "total_passing": len(passing),
            "targets": targets,
            "all_checked": ranked,
        }, f, indent=2, default=str)
    print(f"\n  Results saved to {out_file}")

    # Also save target slugs
    if targets:
        slug_file = OUTPUT_DIR / "daily_targets.txt"
        with open(slug_file, "w") as f:
            f.write(",".join(t["slug"] for t in targets))
        print(f"  Slug list: {slug_file}")


if __name__ == "__main__":
    main()
