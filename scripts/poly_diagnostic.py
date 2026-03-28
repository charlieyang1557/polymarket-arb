#!/usr/bin/env python3
"""
Polymarket US API diagnostic — verify SDK and explore data format.

PUBLIC endpoints only — no auth needed.

Usage:
    python scripts/poly_diagnostic.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from polymarket_us import PolymarketUS

OUTPUT_DIR = Path("data/polymarket_diagnostic")


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
    """Compute spread metrics from parsed book."""
    best_bid = bids[0][0] if bids else 0
    best_ask = asks[0][0] if asks else 0

    if best_bid <= 0 or best_ask <= 0:
        return {"best_bid": best_bid, "best_ask": best_ask,
                "spread": 0, "midpoint": 0,
                "bid_depth": sum(s for _, s in bids),
                "ask_depth": sum(s for _, s in asks)}

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(best_ask - best_bid, 4),
        "midpoint": round((best_bid + best_ask) / 2, 4),
        "bid_depth": sum(s for _, s in bids),
        "ask_depth": sum(s for _, s in asks),
    }


def extract_tags(event: dict) -> list[str]:
    """Extract all tags/series from an event."""
    tags = []
    for t in event.get("tags", []):
        if isinstance(t, dict):
            slug = t.get("slug", "")
            if slug:
                tags.append(slug)
        elif isinstance(t, str) and t:
            tags.append(t)

    series = event.get("seriesSlug", "")
    if series:
        tags.append(series)

    cat = event.get("category", "")
    if cat and cat not in tags:
        tags.append(cat)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            result.append(t)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    client = PolymarketUS()

    print("=" * 70)
    print("POLYMARKET US — API DIAGNOSTIC")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Phase 1: Fetch active events
    print("\n  Phase 1: Fetching 50 active events (by volume desc)...")
    resp = client.events.list({
        "limit": 50,
        "active": True,
        "orderBy": ["volume"],
        "orderDirection": "desc",
    })
    events = resp.get("events", [])
    print(f"  Got {len(events)} events")

    # Collect ALL unique tags/categories
    all_tags = set()
    all_series = set()
    all_categories = set()
    all_market_types = set()
    all_sports_types = set()

    for ev in events:
        for t in extract_tags(ev):
            all_tags.add(t)
        s = ev.get("seriesSlug", "")
        if s:
            all_series.add(s)
        all_categories.add(ev.get("category", ""))
        for m in ev.get("markets", []):
            all_market_types.add(m.get("marketType", ""))
            all_sports_types.add(m.get("sportsMarketType", ""))

    print(f"\n  Categories: {sorted(all_categories)}")
    print(f"  Series: {sorted(all_series)}")
    print(f"  Tags: {sorted(all_tags)}")
    print(f"  Market types: {sorted(all_market_types)}")
    print(f"  Sports market types: {sorted(all_sports_types)}")

    # Print events
    print(f"\n{'=' * 70}")
    print("EVENTS")
    print("=" * 70)
    header = f"{'#':>2} {'Slug':<45} {'Cat':<8} {'Series':<12} {'#Mkts':>5} {'Live':>4} {'Ended':>5}"
    print(header)
    print("-" * len(header))
    for i, ev in enumerate(events[:50], 1):
        slug = ev.get("slug", "")[:44]
        cat = ev.get("category", "")[:7]
        series = ev.get("seriesSlug", "")[:11]
        n_mkts = len(ev.get("markets", []))
        live = "Y" if ev.get("live") else ""
        ended = "Y" if ev.get("ended") else ""
        print(f"{i:2d} {slug:<45} {cat:<8} {series:<12} {n_mkts:5d} {live:>4} {ended:>5}")

    # Phase 2: Top 5 by volume — fetch orderbooks
    print(f"\n{'=' * 70}")
    print("TOP 5 EVENTS — ORDERBOOK DATA")
    print("=" * 70)

    checked = 0
    for ev in events:
        if checked >= 5:
            break
        if ev.get("ended") or ev.get("closed"):
            continue

        print(f"\n  Event: {ev.get('title', '')[:60]}")
        for m in ev.get("markets", [])[:3]:
            slug = m.get("slug", "")
            if not slug:
                continue

            try:
                book_raw = client.markets.book(slug)
                bids, asks = parse_book(book_raw)
                metrics = compute_spread(bids, asks)

                bbo_raw = client.markets.bbo(slug)
                md = bbo_raw.get("marketData", {}) if bbo_raw else {}
                shares = md.get("sharesTraded", "0")
                oi = md.get("openInterest", "0")

                print(f"    {slug[:50]:<50} "
                      f"bid={metrics['best_bid']:.3f} ask={metrics['best_ask']:.3f} "
                      f"spread={metrics['spread']:.3f} mid={metrics['midpoint']:.3f} "
                      f"depth={metrics['bid_depth']:.0f}/{metrics['ask_depth']:.0f} "
                      f"shares={shares} OI={oi}")
            except Exception as e:
                print(f"    {slug[:50]:<50} ERROR: {e}")

            time.sleep(0.1)

        checked += 1

    # Phase 3: Full series inventory
    print(f"\n{'=' * 70}")
    print("ALL SERIES")
    print("=" * 70)

    try:
        series_resp = client.series.list({"limit": 100})
        for s in series_resp.get("series", []):
            print(f"  {s.get('slug', ''):<25} {s.get('title', '')}")
    except Exception as e:
        print(f"  Error fetching series: {e}")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "diagnostic.json"
    with open(out_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sdk_version": "polymarket-us",
            "total_events": len(events),
            "categories": sorted(all_categories),
            "series": sorted(all_series),
            "tags": sorted(all_tags),
            "market_types": sorted(all_market_types),
            "sports_market_types": sorted(all_sports_types),
            "events": [{
                "slug": ev.get("slug"),
                "title": ev.get("title"),
                "category": ev.get("category"),
                "series": ev.get("seriesSlug"),
                "n_markets": len(ev.get("markets", [])),
                "live": ev.get("live"),
                "ended": ev.get("ended"),
            } for ev in events],
        }, f, indent=2)
    print(f"\n  Results saved to {out_file}")


if __name__ == "__main__":
    main()
