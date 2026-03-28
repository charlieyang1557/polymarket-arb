#!/usr/bin/env python3
"""
Polymarket US Live Spread Scan — find MM opportunities.

Fetches active markets via polymarket-us SDK, gets orderbooks,
computes spread/depth/symmetry with maker rebate adjustments.

PUBLIC endpoints only — no auth needed.

Polymarket US fee structure:
  - Sports: taker fee ~2%, maker rebate = 25% of taker fee
  - Crypto: taker fee ~2%, maker rebate = 20% of taker fee
  - Geopolitical: FEE FREE for everyone
  - Currently platform is sports-only (CFTC regulated)

Usage:
    python scripts/poly_full_scan.py
    python scripts/poly_full_scan.py --top 30 --max-check 200
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from polymarket_us import PolymarketUS

OUTPUT_DIR = Path("data/polymarket_diagnostic")

# Fee structure by series prefix
# Currently all sports, but ready for future categories
REBATE_CONFIG = {
    "default": {"taker_fee_pct": 0.02, "rebate_pct": 0.25},  # sports default
    "crypto": {"taker_fee_pct": 0.02, "rebate_pct": 0.20},
    "geopolitical": {"taker_fee_pct": 0.0, "rebate_pct": 0.0},  # fee-free
}


# ---------------------------------------------------------------------------
# Pure helpers (tested)
# ---------------------------------------------------------------------------

def parse_book(raw) -> tuple[list[tuple], list[tuple]]:
    """Parse Polymarket US orderbook: px.value + qty strings."""
    if raw is None:
        return [], []
    md = raw.get("marketData", {}) or {}
    bids = []
    for level in md.get("bids", []):
        try:
            px = float(level["px"]["value"])
            qty = float(level["qty"])
            bids.append((px, qty))
        except (KeyError, ValueError, TypeError):
            continue

    asks = []
    for level in md.get("offers", []):
        try:
            px = float(level["px"]["value"])
            qty = float(level["qty"])
            asks.append((px, qty))
        except (KeyError, ValueError, TypeError):
            continue

    return bids, asks


def compute_spread(bids: list[tuple], asks: list[tuple]) -> dict:
    """Compute spread metrics from parsed book. Spread in cents."""
    best_bid = bids[0][0] if bids else 0
    best_ask = asks[0][0] if asks else 0

    if best_bid <= 0 or best_ask <= 0:
        return {"best_bid": best_bid, "best_ask": best_ask,
                "spread_cents": 0, "midpoint": 0,
                "bid_depth": sum(s for _, s in bids),
                "ask_depth": sum(s for _, s in asks),
                "symmetry": 0}

    spread = best_ask - best_bid
    midpoint = (best_bid + best_ask) / 2
    bid_depth = sum(s for _, s in bids)
    ask_depth = sum(s for _, s in asks)

    if bid_depth > 0 and ask_depth > 0:
        symmetry = round(bid_depth / ask_depth, 3)
    else:
        symmetry = 0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_cents": round(spread * 100, 1),
        "midpoint": round(midpoint, 4),
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "symmetry": symmetry,
    }


def compute_net_spread(spread_cents: float, midpoint: float,
                        rebate_pct: float, taker_fee_pct: float) -> float:
    """Net spread including maker rebate income.

    Maker captures the spread AND gets a rebate on each fill:
      rebate_per_side = taker_fee_pct * midpoint * (1-midpoint) * 100 * rebate_pct
      net_spread = gross_spread + 2 * rebate_per_side  (round trip)

    For fee-free markets: net = gross.
    """
    if spread_cents == 0:
        return 0

    if taker_fee_pct == 0:
        return spread_cents

    # Taker fee per contract per side (in cents)
    taker_fee_per_side = taker_fee_pct * midpoint * (1 - midpoint) * 100
    rebate_per_side = taker_fee_per_side * rebate_pct

    return round(spread_cents + 2 * rebate_per_side, 2)


def extract_scannable_markets(markets: list[dict],
                               min_shares: int = 0) -> list[dict]:
    """Filter to active, open markets worth scanning."""
    results = []
    for m in markets:
        if m.get("closed"):
            continue
        if not m.get("active", True):
            continue

        shares = float(m.get("_shares_traded", 0) or 0)
        if shares < min_shares:
            continue

        results.append({
            "slug": m.get("slug", ""),
            "question": (m.get("question") or "")[:80],
            "market_type": m.get("marketType", ""),
            "series_slug": m.get("seriesSlug", ""),
            "category": m.get("category", ""),
            "shares_traded": shares,
        })

    return results


def _get_rebate_config(series_slug: str) -> dict:
    """Look up fee/rebate config for a series."""
    slug_lower = (series_slug or "").lower()
    for key in REBATE_CONFIG:
        if key != "default" and key in slug_lower:
            return REBATE_CONFIG[key]
    return REBATE_CONFIG["default"]


def build_series_summary(candidates: list[dict]) -> list[dict]:
    """Aggregate spread/depth metrics by series."""
    by_series = defaultdict(list)
    for c in candidates:
        by_series[c.get("series_slug", "unknown")].append(c)

    summary = []
    for series, markets in by_series.items():
        spreads = [m["spread_cents"] for m in markets if m.get("spread_cents")]
        nets = [m["net_spread_cents"] for m in markets if m.get("net_spread_cents")]
        depths = [m["bid_depth"] + m["ask_depth"] for m in markets
                  if m.get("bid_depth") is not None]
        syms = [m["symmetry"] for m in markets
                if m.get("symmetry") and m["symmetry"] > 0]

        summary.append({
            "series": series,
            "markets": len(markets),
            "avg_spread_cents": round(sum(spreads) / max(len(spreads), 1), 1),
            "avg_net_spread_cents": round(sum(nets) / max(len(nets), 1), 1),
            "avg_depth": round(sum(depths) / max(len(depths), 1), 0),
            "avg_symmetry": round(sum(syms) / max(len(syms), 1), 3),
        })

    summary.sort(key=lambda s: s["markets"], reverse=True)
    return summary


# ---------------------------------------------------------------------------
# Main (SDK-dependent)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket US live spread scan")
    parser.add_argument("--top", type=int, default=30,
                        help="Number of top markets to show (default: 30)")
    parser.add_argument("--max-check", type=int, default=200,
                        help="Max markets to deep-check (default: 200)")
    parser.add_argument("--min-shares", type=int, default=50,
                        help="Min shares traded (default: 50)")
    args = parser.parse_args()

    client = PolymarketUS()

    print("=" * 70)
    print("POLYMARKET US — LIVE SPREAD SCAN")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Phase 1: Fetch active open markets
    print(f"\n  Phase 1: Fetching active markets...")
    all_markets = []
    offset = 0
    page_size = 100

    while True:
        resp = client.markets.list({
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
        print(f"    ... {len(all_markets)} markets fetched")
        time.sleep(0.1)

    print(f"  Total active open markets: {len(all_markets)}")

    # Phase 2: Enrich with BBO (sharesTraded, OI)
    print(f"\n  Phase 2: Fetching BBO for volume/depth info...")
    for i, m in enumerate(all_markets):
        slug = m.get("slug", "")
        if not slug:
            continue
        try:
            bbo = client.markets.bbo(slug)
            md = bbo.get("marketData", {}) if bbo else {}
            m["_shares_traded"] = float(md.get("sharesTraded", "0") or "0")
            m["_open_interest"] = float(md.get("openInterest", "0") or "0")
            m["_best_bid"] = float(md.get("bestBid", {}).get("value", "0") or "0") if md.get("bestBid") else 0
            m["_best_ask"] = float(md.get("bestAsk", {}).get("value", "0") or "0") if md.get("bestAsk") else 0
        except Exception:
            m["_shares_traded"] = 0
            m["_open_interest"] = 0

        if (i + 1) % 100 == 0:
            print(f"    ... {i + 1}/{len(all_markets)}")
        time.sleep(0.05)

    # Filter to scannable
    scannable = extract_scannable_markets(all_markets, min_shares=args.min_shares)
    print(f"  {len(scannable)} markets with shares >= {args.min_shares}")

    if not scannable:
        print("\n  No markets to scan.")
        return

    # Phase 3: Fetch full orderbooks for top markets
    to_check = scannable[:args.max_check]
    print(f"\n  Phase 3: Fetching orderbooks for {len(to_check)} markets...")

    candidates = []
    empty_books = 0

    for i, m in enumerate(to_check):
        slug = m["slug"]
        try:
            book_raw = client.markets.book(slug)
            bids, asks = parse_book(book_raw)

            if not bids and not asks:
                empty_books += 1
                continue

            metrics = compute_spread(bids, asks)

            # Get fee/rebate config for this series
            rc = _get_rebate_config(m.get("series_slug", ""))
            net = compute_net_spread(
                metrics["spread_cents"], metrics["midpoint"],
                rc["rebate_pct"], rc["taker_fee_pct"])

            candidates.append({
                **m,
                **metrics,
                "net_spread_cents": net,
                "rebate_pct": rc["rebate_pct"],
                "taker_fee_pct": rc["taker_fee_pct"],
            })

        except Exception as e:
            empty_books += 1

        if (i + 1) % 50 == 0:
            print(f"    ... {i + 1}/{len(to_check)}")
        time.sleep(0.05)

    print(f"  {len(candidates)} markets with live books "
          f"({empty_books} empty/error)")

    if not candidates:
        print("\n  No markets with live orderbooks.")
        return

    # Report A: Series summary
    print(f"\n{'=' * 70}")
    print("REPORT A: Series Summary")
    print("=" * 70)

    series_sum = build_series_summary(candidates)
    header = (f"{'Series':<20} {'Mkts':>5} {'Avg Sprd':>8} {'Avg Net':>7} "
              f"{'Avg Depth':>9} {'Avg Sym':>7}")
    print(header)
    print("-" * len(header))
    for s in series_sum:
        print(f"{s['series']:<20} {s['markets']:5d} "
              f"{s['avg_spread_cents']:8.1f}c {s['avg_net_spread_cents']:6.1f}c "
              f"{s['avg_depth']:9.0f} {s['avg_symmetry']:7.3f}")

    # Report B: Top by tightest spread
    print(f"\n{'=' * 70}")
    print(f"REPORT B: Top {args.top} Tightest Spreads")
    print("=" * 70)

    tight = sorted(candidates, key=lambda c: c["spread_cents"]
                   if c["spread_cents"] > 0 else 999)[:args.top]
    header2 = (f"{'#':>2} {'Series':<12} {'Type':<10} {'Sprd':>5} {'Net':>5} "
               f"{'Mid':>6} {'Depth':>8} {'Sym':>5} {'Shares':>8} {'Question':<35}")
    print(header2)
    print("-" * len(header2))
    for i, c in enumerate(tight, 1):
        series = (c.get("series_slug") or "")[:11]
        mt = (c.get("market_type") or "")[:9]
        depth = c.get("bid_depth", 0) + c.get("ask_depth", 0)
        sym = c.get("symmetry", 0)
        shares = c.get("shares_traded", 0)
        print(f"{i:2d} {series:<12} {mt:<10} {c['spread_cents']:5.1f} "
              f"{c['net_spread_cents']:5.1f} {c['midpoint']:6.3f} "
              f"{depth:8.0f} {sym:5.2f} {shares:8.0f} "
              f"{c['question'][:35]}")

    # Report C: Top by widest spread (most room)
    print(f"\n{'=' * 70}")
    print(f"REPORT C: Top {args.top} Widest Spreads (most profit room)")
    print("=" * 70)

    wide = sorted(candidates, key=lambda c: c["spread_cents"], reverse=True)[:args.top]
    print(header2)
    print("-" * len(header2))
    for i, c in enumerate(wide, 1):
        series = (c.get("series_slug") or "")[:11]
        mt = (c.get("market_type") or "")[:9]
        depth = c.get("bid_depth", 0) + c.get("ask_depth", 0)
        sym = c.get("symmetry", 0)
        shares = c.get("shares_traded", 0)
        print(f"{i:2d} {series:<12} {mt:<10} {c['spread_cents']:5.1f} "
              f"{c['net_spread_cents']:5.1f} {c['midpoint']:6.3f} "
              f"{depth:8.0f} {sym:5.2f} {shares:8.0f} "
              f"{c['question'][:35]}")

    # Report D: MM Sweet Spot
    print(f"\n{'=' * 70}")
    print("REPORT D: MM Sweet Spot (spread 2-10c, depth>50, sym 0.3-3.0)")
    print("=" * 70)

    sweet = [c for c in candidates
             if 2 <= c.get("spread_cents", 0) <= 10
             and (c.get("bid_depth", 0) + c.get("ask_depth", 0)) > 50
             and 0.3 <= c.get("symmetry", 0) <= 3.0]
    sweet.sort(key=lambda c: c.get("net_spread_cents", 0), reverse=True)

    if sweet:
        print(f"  {len(sweet)} markets in sweet spot")
        print(header2)
        print("-" * len(header2))
        for i, c in enumerate(sweet[:args.top], 1):
            series = (c.get("series_slug") or "")[:11]
            mt = (c.get("market_type") or "")[:9]
            depth = c.get("bid_depth", 0) + c.get("ask_depth", 0)
            sym = c.get("symmetry", 0)
            shares = c.get("shares_traded", 0)
            print(f"{i:2d} {series:<12} {mt:<10} {c['spread_cents']:5.1f} "
                  f"{c['net_spread_cents']:5.1f} {c['midpoint']:6.3f} "
                  f"{depth:8.0f} {sym:5.2f} {shares:8.0f} "
                  f"{c['question'][:35]}")
    else:
        print("  No markets in sweet spot")

    # Report E: Kalshi comparison guide
    print(f"\n{'=' * 70}")
    print("REPORT E: Kalshi vs Polymarket US Comparison")
    print("=" * 70)
    print("  Kalshi: maker fee = ceil(1.75% * P*(1-P) * 100) per side")
    print("  Polymarket US: maker REBATE = 25% of taker fee (sports)")
    print("  → Polymarket makers are PAID; Kalshi makers are TAXED")
    print()

    if candidates:
        avg_spread = sum(c["spread_cents"] for c in candidates) / len(candidates)
        avg_net = sum(c["net_spread_cents"] for c in candidates) / len(candidates)
        avg_depth = sum(c["bid_depth"] + c["ask_depth"]
                        for c in candidates) / len(candidates)
        print(f"  Polymarket US averages (N={len(candidates)}):")
        print(f"    Avg spread: {avg_spread:.1f}c")
        print(f"    Avg net spread (with rebate): {avg_net:.1f}c")
        print(f"    Avg total depth: {avg_depth:.0f} shares")
        print()
        print("  Kalshi sports averages (from earlier scans):")
        print("    Avg spread: 2-3c")
        print("    Avg net spread (after fees): 0-1c")
        print("    Avg total depth: 1000-5000 contracts")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_file = OUTPUT_DIR / f"full_scan_{date_str}.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_active": len(all_markets),
            "total_scannable": len(scannable),
            "total_with_books": len(candidates),
            "series_summary": series_sum,
            "top_tight": tight,
            "top_wide": wide[:args.top],
            "sweet_spot": sweet[:args.top],
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
