"""
Kalshi Market Maker Candidate Scanner.

Scans ALL active Kalshi markets and ranks by MM-suitability metrics:
1. 24h trade volume (need > 1000 contracts/day)
2. Bid-ask spread (need >= 2c for profitability)
3. Queue symmetry: YES_depth / NO_depth (need 0.3-3.0)
4. Trade frequency: trades per hour (need > 20)
5. Category (sports, economics, politics)

Outputs ranked table + JSON to data/kalshi_diagnostic/mm_candidates.json

Usage:
    python scripts/kalshi_mm_scanner.py              # production
    python scripts/kalshi_mm_scanner.py --demo       # demo environment
    python scripts/kalshi_mm_scanner.py --top 30     # show top 30
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, DEMO_BASE, PROD_BASE

load_dotenv()
OUTPUT_DIR = Path("data/kalshi_diagnostic")

# Rate limit: 20 reads/sec. Stay at ~10/sec to be safe.
RATE_LIMIT_DELAY = 0.1  # seconds between API calls


def save_json(data, filename):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved -> {filepath}")


def rate_limit():
    time.sleep(RATE_LIMIT_DELAY)


# ---------------------------------------------------------------------------
# Phase 1: Fetch events with nested markets (much fewer API calls than
# paginating 60k+ individual markets)
# ---------------------------------------------------------------------------

def fetch_events_with_markets(client: KalshiClient) -> tuple[list[dict], dict[str, str]]:
    """Fetch all open events with nested markets.

    Returns (all_markets, event_ticker_to_category).
    Events API returns ~100 events/page with nested markets,
    vs the markets API which returns 60k+ individual markets.
    """
    all_markets = []
    categories = {}
    cursor = None
    page = 0

    while True:
        page += 1
        data = client.get_events(limit=100, with_nested_markets=True,
                                 status="open", cursor=cursor)
        batch = data.get("events", [])

        for ev in batch:
            event_ticker = ev.get("event_ticker", "")
            category = ev.get("category", "unknown")
            categories[event_ticker] = category

            for m in ev.get("markets", []):
                m["_category"] = category
                all_markets.append(m)

        cursor = data.get("cursor")
        print(f"  Page {page}: {len(batch)} events, "
              f"{sum(len(e.get('markets', [])) for e in batch)} markets "
              f"(total: {len(all_markets)} markets)")
        rate_limit()

        if not cursor or not batch:
            break

    return all_markets, categories


# ---------------------------------------------------------------------------
# Phase 2: Filter candidates from market-level data
# ---------------------------------------------------------------------------

def _cents(m: dict, field: str) -> int:
    """Extract price in cents, handling both integer and _dollars string formats."""
    # Try integer field first (from markets API)
    val = m.get(field)
    if val is not None and val != "":
        return int(val)
    # Fall back to _dollars string (from events API nested markets)
    dollars_str = m.get(f"{field}_dollars", "0") or "0"
    return int(round(float(dollars_str) * 100))


def prefilter_markets(markets: list[dict], categories: dict[str, str]) -> list[dict]:
    """Fast filter using market-level data (no extra API calls).

    Keep markets with:
    - Both YES bid and YES ask > 0 (has two-sided market)
    - Spread >= 2c
    - Not MVE/parlay markets (complex, not suitable for simple MM)
    """
    candidates = []

    for m in markets:
        ticker = m.get("ticker", "")

        # Skip MVE/parlay markets
        if "KXMVE" in ticker or m.get("mve_collection_ticker"):
            continue

        yes_bid = _cents(m, "yes_bid")
        yes_ask = _cents(m, "yes_ask")
        no_bid = _cents(m, "no_bid")

        # Need two-sided quotes
        if yes_bid <= 0 or yes_ask <= 0:
            continue

        spread = yes_ask - yes_bid
        if spread < 2:
            continue

        vol_24h = int(float(m.get("volume_24h_fp", "0") or "0"))

        # Get category — from nested event data or lookup
        event_ticker = m.get("event_ticker", "")
        category = m.get("_category") or categories.get(event_ticker, "unknown")

        # Infer category from ticker prefix if still unknown
        if category == "unknown":
            if any(x in ticker for x in ["NBA", "NFL", "NHL", "MLB", "NCAA",
                                          "SOCCER", "UFC", "BOXING"]):
                category = "Sports"
            elif any(x in ticker for x in ["CPI", "GDP", "JOBS", "FED",
                                           "FOMC", "RATE", "INFLATION"]):
                category = "Economics"

        candidates.append({
            "ticker": ticker,
            "title": (m.get("title") or m.get("yes_sub_title") or "")[:80],
            "event_ticker": event_ticker,
            "category": category,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": _cents(m, "no_ask"),
            "spread": spread,
            "midpoint": (yes_bid + yes_ask) / 2,
            "volume_24h": vol_24h,
            "volume_total": int(float(m.get("volume_fp", "0") or "0")),
            "open_interest": int(float(m.get("open_interest_fp", "0") or "0")),
            "yes_bid_size": int(float(m.get("yes_bid_size_fp", "0") or "0")),
            "yes_ask_size": int(float(m.get("yes_ask_size_fp", "0") or "0")),
            "close_time": m.get("close_time", ""),
            "expected_expiration_time": m.get("expected_expiration_time", ""),
        })

    # Sort by 24h volume descending, then total volume
    candidates.sort(key=lambda c: (c["volume_24h"], c["volume_total"]),
                    reverse=True)

    return candidates


# ---------------------------------------------------------------------------
# Phase 3: Deep scan — orderbooks + trades for top candidates
# ---------------------------------------------------------------------------

def _parse_book_levels(book: dict) -> tuple[list, list]:
    """Parse orderbook into [(price_cents, size), ...] for YES and NO.

    Handles both formats:
    - Production: {"orderbook_fp": {"yes_dollars": [["0.25", "100.00"], ...], ...}}
    - Demo/old:   {"orderbook": {"yes": [[25, 100], ...], ...}}
    """
    # Try orderbook_fp format first (production)
    ob = book.get("orderbook_fp") or book.get("orderbook") or book

    # Try _dollars keys (string format)
    yes_raw = ob.get("yes_dollars") or ob.get("yes", [])
    no_raw = ob.get("no_dollars") or ob.get("no", [])

    def to_cents_levels(levels):
        result = []
        for lvl in levels:
            if isinstance(lvl[0], str):
                price = int(round(float(lvl[0]) * 100))
                size = int(float(lvl[1]))
            else:
                price = int(lvl[0])
                size = int(lvl[1])
            result.append((price, size))
        return result

    return to_cents_levels(yes_raw), to_cents_levels(no_raw)


def fetch_orderbook_metrics(client: KalshiClient, ticker: str) -> dict:
    """Fetch orderbook and compute depth/symmetry metrics."""
    try:
        data = client.get_orderbook(ticker, depth=20)
    except Exception as e:
        return {"error": str(e)}

    yes_levels, no_levels = _parse_book_levels(data)

    yes_depth = sum(lvl[1] for lvl in yes_levels) if yes_levels else 0
    no_depth = sum(lvl[1] for lvl in no_levels) if no_levels else 0

    # Best bid (highest price level — levels sorted ascending)
    best_yes_bid = yes_levels[-1][0] if yes_levels else 0
    best_no_bid = no_levels[-1][0] if no_levels else 0

    # Depth at best bid specifically
    yes_best_depth = yes_levels[-1][1] if yes_levels else 0
    no_best_depth = no_levels[-1][1] if no_levels else 0

    # Queue symmetry ratio
    if no_depth > 0 and yes_depth > 0:
        symmetry_ratio = yes_depth / no_depth
    elif yes_depth > 0:
        symmetry_ratio = 999.0  # only YES side
    elif no_depth > 0:
        symmetry_ratio = 0.001  # only NO side
    else:
        symmetry_ratio = 0.0  # empty book

    return {
        "yes_depth_total": yes_depth,
        "no_depth_total": no_depth,
        "yes_best_depth": yes_best_depth,
        "no_best_depth": no_best_depth,
        "yes_levels": len(yes_levels),
        "no_levels": len(no_levels),
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "symmetry_ratio": round(symmetry_ratio, 3),
    }


def fetch_trade_frequency(client: KalshiClient, ticker: str) -> dict:
    """Fetch recent trades and compute frequency metrics."""
    try:
        # Get last 200 trades to measure frequency
        data = client.get_trades(ticker, limit=200)
        trades = data.get("trades", [])
    except Exception as e:
        return {"error": str(e), "trades_24h": 0, "trades_per_hour": 0,
                "volume_from_trades": 0}

    if not trades:
        return {"trades_24h": 0, "trades_per_hour": 0,
                "volume_from_trades": 0}

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    trades_24h = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00"))
            if ts >= cutoff_24h:
                trades_24h.append({
                    "ts": ts,
                    "count": int(float(t.get("count_fp", "1"))),
                })
        except (KeyError, ValueError):
            continue

    total_contracts_24h = sum(t["count"] for t in trades_24h)
    num_trades_24h = len(trades_24h)

    # Trades per hour (based on actual time span of trades)
    if num_trades_24h >= 2:
        oldest = min(t["ts"] for t in trades_24h)
        newest = max(t["ts"] for t in trades_24h)
        span_hours = max((newest - oldest).total_seconds() / 3600, 0.01)
        trades_per_hour = num_trades_24h / span_hours
    elif num_trades_24h == 1:
        trades_per_hour = 1.0
    else:
        trades_per_hour = 0.0

    return {
        "trades_24h": num_trades_24h,
        "trades_per_hour": round(trades_per_hour, 1),
        "volume_from_trades": total_contracts_24h,
    }


def deep_scan(client: KalshiClient, candidates: list[dict],
              max_candidates: int = 80) -> list[dict]:
    """Fetch orderbooks and trades for top candidates."""
    top = candidates[:max_candidates]
    total = len(top)

    print(f"\n  Deep scanning {total} candidates (orderbook + trades)...")
    print(f"  Estimated time: ~{total * 2 * RATE_LIMIT_DELAY:.0f}s "
          f"({total * 2} API calls)")

    for i, c in enumerate(top):
        ticker = c["ticker"]
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  [{i+1}/{total}] {ticker}")

        # Orderbook
        ob = fetch_orderbook_metrics(client, ticker)
        rate_limit()

        if "error" not in ob:
            c.update(ob)
        else:
            c["symmetry_ratio"] = 0.0
            c["yes_depth_total"] = 0
            c["no_depth_total"] = 0
            c["ob_error"] = ob["error"]

        # Trades
        tf = fetch_trade_frequency(client, ticker)
        rate_limit()
        c.update(tf)

    return top


# ---------------------------------------------------------------------------
# Phase 4: Score and rank
# ---------------------------------------------------------------------------

def compute_mm_score(c: dict) -> float:
    """Composite MM suitability score (higher = better).

    Weights:
    - 30% volume (log-scaled, 1000+ is baseline)
    - 25% spread (2c minimum, 3-5c sweet spot)
    - 25% queue symmetry (0.3-3.0 range, penalize asymmetry)
    - 20% trade frequency (20+/hr baseline)
    """
    import math

    # Volume score: log-scaled, 0 at vol=100, 1.0 at vol=1000, ~1.5 at 10000
    vol = max(c.get("volume_from_trades", 0), c.get("volume_24h", 0))
    vol_score = max(0, math.log10(max(vol, 1)) - 2) if vol > 0 else 0

    # Spread score: 0 below 2c, peak at 3-5c, slight decline above 10c
    spread = c.get("spread", 0)
    if spread < 2:
        spread_score = 0
    elif spread <= 5:
        spread_score = spread / 5
    elif spread <= 10:
        spread_score = 1.0
    else:
        spread_score = max(0.5, 1.0 - (spread - 10) / 20)

    # Symmetry score: 1.0 when ratio is 1.0, 0 when < 0.2 or > 5.0
    sym = c.get("symmetry_ratio", 0)
    if 0.2 <= sym <= 5.0:
        # Score peaks at 1.0, drops toward edges
        if sym <= 1.0:
            sym_score = sym  # 0.2 -> 0.2, 1.0 -> 1.0
        else:
            sym_score = max(0, 2.0 - sym)  # 1.0 -> 1.0, 2.0 -> 0, capped at 0
            sym_score = max(sym_score, 1.0 / sym)  # alternative: 1/ratio
    else:
        sym_score = 0

    # Trade frequency score: 0 below 5/hr, 1.0 at 20/hr, cap at 2.0
    freq = c.get("trades_per_hour", 0)
    freq_score = min(2.0, freq / 20) if freq > 0 else 0

    score = (0.30 * vol_score +
             0.25 * spread_score +
             0.25 * sym_score +
             0.20 * freq_score)

    return round(score, 4)


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Score and rank all candidates."""
    for c in candidates:
        c["mm_score"] = compute_mm_score(c)

        # Flag: passes all hard filters?
        sym = c.get("symmetry_ratio", 0)
        vol = max(c.get("volume_from_trades", 0), c.get("volume_24h", 0))
        c["passes_filters"] = (
            c.get("spread", 0) >= 2
            and 0.2 <= sym <= 5.0
            and vol >= 100  # relaxed from 1000 for discovery
            and c.get("trades_per_hour", 0) >= 5  # relaxed from 20
        )

    candidates.sort(key=lambda c: c["mm_score"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Phase 5: Output
# ---------------------------------------------------------------------------

def print_table(candidates: list[dict], top_n: int = 20):
    """Print ranked table of MM candidates."""
    print("\n" + "=" * 130)
    print("TOP MM CANDIDATES — Ranked by composite score")
    print("=" * 130)
    print(f"{'#':>3} {'Pass':>4} {'Score':>5} {'Ticker':<40} "
          f"{'Cat':<12} {'Sprd':>4} {'Vol24h':>7} {'Trd/hr':>6} "
          f"{'Sym':>5} {'Y_dep':>5} {'N_dep':>5} {'Mid':>4}")
    print("-" * 130)

    for i, c in enumerate(candidates[:top_n], 1):
        flag = " OK " if c.get("passes_filters") else "FAIL"
        sym = c.get("symmetry_ratio", 0)
        sym_str = f"{sym:.2f}" if sym < 100 else ">100"
        vol = max(c.get("volume_from_trades", 0), c.get("volume_24h", 0))

        print(f"{i:3d} {flag:>4} {c['mm_score']:5.3f} "
              f"{c['ticker']:<40} "
              f"{c['category']:<12} "
              f"{c['spread']:4d} "
              f"{vol:7d} "
              f"{c.get('trades_per_hour', 0):6.1f} "
              f"{sym_str:>5} "
              f"{c.get('yes_depth_total', 0):5d} "
              f"{c.get('no_depth_total', 0):5d} "
              f"{c.get('midpoint', 0):4.0f}")

    # Summary stats
    passing = [c for c in candidates if c.get("passes_filters")]
    print(f"\n  Total scanned: {len(candidates)}")
    print(f"  Passing all filters: {len(passing)}")

    # Category breakdown of passing
    cat_counts = {}
    for c in passing:
        cat = c.get("category", "unknown")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    if cat_counts:
        print(f"  By category: {cat_counts}")

    # Highlight top tier
    tier1 = [c for c in passing if c["mm_score"] >= 0.5]
    if tier1:
        print(f"\n  TIER 1 (score >= 0.5): {len(tier1)} markets")
        for c in tier1:
            vol = max(c.get("volume_from_trades", 0), c.get("volume_24h", 0))
            print(f"    {c['ticker']:<40} score={c['mm_score']:.3f} "
                  f"spread={c['spread']}c vol={vol} "
                  f"freq={c.get('trades_per_hour', 0):.0f}/hr "
                  f"sym={c.get('symmetry_ratio', 0):.2f}")


def print_failed_filter_analysis(candidates: list[dict]):
    """Show why markets fail filters — useful for tuning thresholds."""
    print("\n" + "=" * 80)
    print("FILTER FAILURE ANALYSIS (top 20 by volume that fail)")
    print("=" * 80)

    # Markets with volume but failing other filters
    by_vol = sorted(candidates, key=lambda c: max(
        c.get("volume_from_trades", 0), c.get("volume_24h", 0)), reverse=True)
    failed = [c for c in by_vol if not c.get("passes_filters")][:20]

    for c in failed:
        sym = c.get("symmetry_ratio", 0)
        vol = max(c.get("volume_from_trades", 0), c.get("volume_24h", 0))
        reasons = []
        if c.get("spread", 0) < 2:
            reasons.append(f"spread={c['spread']}c<2")
        if sym < 0.2:
            reasons.append(f"sym={sym:.3f}<0.2 (NO-heavy)")
        elif sym > 5.0:
            reasons.append(f"sym={sym:.1f}>5.0 (YES-heavy)")
        if vol < 100:
            reasons.append(f"vol={vol}<100")
        if c.get("trades_per_hour", 0) < 5:
            reasons.append(f"freq={c.get('trades_per_hour', 0):.1f}<5/hr")

        if reasons:
            print(f"  {c['ticker']:<40} vol={vol:>6} FAIL: {', '.join(reasons)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan Kalshi markets for MM candidates")
    parser.add_argument("--demo", action="store_true",
                        help="Use demo environment")
    parser.add_argument("--top", type=int, default=20,
                        help="Number of top candidates to show (default: 20)")
    parser.add_argument("--deep-scan-limit", type=int, default=80,
                        help="Max markets for deep scan (default: 80)")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    base_url = DEMO_BASE if args.demo else PROD_BASE
    env_name = "DEMO" if args.demo else "PRODUCTION"

    print("=" * 60)
    print("Kalshi MM Candidate Scanner")
    print(f"Environment: {env_name}")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    client = KalshiClient(api_key, pk_path, base_url)

    # Phase 1: Fetch events with nested markets (much faster than
    # paginating 60k+ individual markets)
    print("\n--- Phase 1: Fetching events with nested markets ---")
    all_markets, categories = fetch_events_with_markets(client)
    print(f"\n  Total markets from events: {len(all_markets)}")
    print(f"  Total event categories: {len(categories)}")

    # Phase 2: Pre-filter using market-level data
    print("\n--- Phase 2: Pre-filtering candidates ---")
    candidates = prefilter_markets(all_markets, categories)
    print(f"  Candidates after pre-filter: {len(candidates)} "
          f"(from {len(all_markets)} total)")

    if not candidates:
        print("\n  No candidates found. Try --demo for demo environment.")
        return

    # Phase 3: Deep scan top candidates
    print("\n--- Phase 3: Deep scan (orderbooks + trades) ---")
    scanned = deep_scan(client, candidates, args.deep_scan_limit)

    # Phase 4: Score and rank
    print("\n--- Phase 4: Scoring and ranking ---")
    ranked = rank_candidates(scanned)

    # Phase 5: Output
    print_table(ranked, args.top)
    print_failed_filter_analysis(ranked)

    # Save results
    save_json(ranked, "mm_candidates.json")

    # Also save a compact version with just the key metrics
    compact = []
    for i, c in enumerate(ranked):
        vol = max(c.get("volume_from_trades", 0), c.get("volume_24h", 0))
        compact.append({
            "rank": i + 1,
            "ticker": c["ticker"],
            "title": c["title"],
            "category": c["category"],
            "mm_score": c["mm_score"],
            "passes_filters": c["passes_filters"],
            "spread_cents": c["spread"],
            "midpoint_cents": c["midpoint"],
            "volume_24h": vol,
            "trades_per_hour": c.get("trades_per_hour", 0),
            "symmetry_ratio": c.get("symmetry_ratio", 0),
            "yes_depth": c.get("yes_depth_total", 0),
            "no_depth": c.get("no_depth_total", 0),
            "open_interest": c.get("open_interest", 0),
        })
    save_json(compact, "mm_candidates_compact.json")

    print(f"\n  Done. Results in {OUTPUT_DIR}/mm_candidates*.json")


if __name__ == "__main__":
    main()
