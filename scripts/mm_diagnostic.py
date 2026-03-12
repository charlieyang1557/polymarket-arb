#!/usr/bin/env python3
"""
Market Making Target Diagnostic for Kalshi.

Analyzes top markets to find ideal MM targets by measuring:
- Spread (want >= 2¢)
- Queue depth at best bid vs hourly trade volume (want < 1h wait)
- Net edge after maker fees (want > 0)
- Daily volume (want > $10k equivalent)

Usage:
    python scripts/mm_diagnostic.py
    python scripts/mm_diagnostic.py --top 50   # analyze more markets
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, PROD_BASE

load_dotenv()
OUTPUT_DIR = Path("data/kalshi_diagnostic")


def save_json(data, filename):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved → {path}")


# ---------------------------------------------------------------------------
# Step 1: Fetch all active markets
# ---------------------------------------------------------------------------

def fetch_active_markets(client, pages=15):
    """Fetch individual markets via events endpoint (avoids MVE/parlay flood)."""
    all_markets = []
    cursor = None
    for page in range(pages):
        data = client.get_events(limit=20, status="open", cursor=cursor)
        events = data.get("events", [])
        for event in events:
            cat = event.get("category", "?")
            event_title = event.get("title", "?")
            for m in event.get("markets", []):
                m["category"] = cat
                m["_event_title"] = event_title
                all_markets.append(m)
        cursor = data.get("cursor")
        if not cursor or not events:
            break
        print(f"    page {page+1}: {len(events)} events, "
              f"{len(all_markets)} markets so far")

    for m in all_markets:
        m["_vol"] = float(m.get("volume_fp", 0) or 0)
        m["_vol24h"] = float(m.get("volume_24h_fp", 0) or 0)

    # Filter out MVE/parlay markets
    individual = [m for m in all_markets
                  if not m.get("ticker", "").startswith("KXMVE")]
    print(f"    {len(all_markets)} total → {len(individual)} individual markets"
          f" (filtered {len(all_markets) - len(individual)} MVE)")

    return sorted(individual, key=lambda m: m["_vol"], reverse=True)


# ---------------------------------------------------------------------------
# Step 2: Analyze individual market
# ---------------------------------------------------------------------------

def analyze_market(client, m):
    """Fetch orderbook + trades, compute MM metrics."""
    ticker = m["ticker"]

    # --- Orderbook ---
    try:
        data = client.get_orderbook(ticker, depth=20)
        book = data.get("orderbook", data)
    except Exception as e:
        return {"ticker": ticker, "error": f"orderbook: {e}"}

    yes_bids = book.get("yes", [])
    no_bids = book.get("no", [])

    if not yes_bids or not no_bids:
        return {"ticker": ticker, "error": "one-sided or empty book"}

    best_yes_bid = yes_bids[-1][0]   # highest YES bid (cents)
    best_no_bid = no_bids[-1][0]     # highest NO bid (cents)
    yes_ask = 100 - best_no_bid      # implied YES ask
    spread = yes_ask - best_yes_bid

    # Depth at best bid price level
    yes_depth_best = sum(q for p, q in yes_bids if p == best_yes_bid)
    no_depth_best = sum(q for p, q in no_bids if p == best_no_bid)
    total_yes_depth = sum(q for _, q in yes_bids)
    total_no_depth = sum(q for _, q in no_bids)

    # --- Recent trades ---
    try:
        trade_data = client.get_trades(ticker, limit=500)
        trades = trade_data.get("trades", [])
    except Exception:
        trades = []

    hourly_vol = 0.0
    hourly_count = 0.0
    vol_at_yes_bid = 0.0
    vol_at_no_bid = 0.0
    time_span_hours = 0.0
    taker_yes_pct = 0.0

    if trades:
        now = datetime.now(timezone.utc)
        parsed = []
        for t in trades:
            try:
                ts = datetime.fromisoformat(
                    t["created_time"].replace("Z", "+00:00"))
                count = float(t.get("count_fp", 0) or 0)
                yes_p = round(float(t.get("yes_price_dollars", 0) or 0) * 100)
                side = t.get("taker_side", "")
                parsed.append((ts, count, yes_p, side))
            except Exception:
                pass

        if parsed:
            oldest = min(p[0] for p in parsed)
            time_span_hours = max((now - oldest).total_seconds() / 3600, 0.01)
            total_vol = sum(p[1] for p in parsed)
            hourly_vol = total_vol / time_span_hours
            hourly_count = len(parsed) / time_span_hours

            # Volume that traded at or near best bid price (±1¢)
            vol_at_yes_bid = sum(
                p[1] for p in parsed
                if abs(p[2] - best_yes_bid) <= 1)
            vol_at_no_bid = sum(
                p[1] for p in parsed
                if abs((100 - p[2]) - best_no_bid) <= 1)

            yes_takers = sum(1 for p in parsed if p[3] == "yes")
            taker_yes_pct = yes_takers / len(parsed) * 100 if parsed else 50

    # --- Fee math ---
    p = best_yes_bid / 100  # price in dollars
    maker_fee = 0.0175 * p * (1 - p) * 100     # cents per contract
    taker_fee = 0.07 * p * (1 - p) * 100

    # Net edge: spread minus maker fees on both legs
    net_edge = spread - 2 * maker_fee

    # Queue wait: how long to fill at best bid given trade volume there
    queue_wait_yes = (yes_depth_best / (vol_at_yes_bid / time_span_hours)
                      if vol_at_yes_bid > 0 and time_span_hours > 0
                      else float("inf"))
    queue_wait_no = (no_depth_best / (vol_at_no_bid / time_span_hours)
                     if vol_at_no_bid > 0 and time_span_hours > 0
                     else float("inf"))

    return {
        "ticker": ticker,
        "title": m.get("title", m.get("yes_sub_title", "?"))[:60],
        "category": m.get("category", "?"),
        "event_ticker": m.get("event_ticker", "?"),
        # Book
        "best_yes_bid": best_yes_bid,
        "yes_ask": yes_ask,
        "best_no_bid": best_no_bid,
        "spread": spread,
        "yes_depth_best": yes_depth_best,
        "no_depth_best": no_depth_best,
        "total_yes_depth": total_yes_depth,
        "total_no_depth": total_no_depth,
        # Trades
        "trades_fetched": len(trades),
        "time_span_hours": round(time_span_hours, 1),
        "hourly_vol": round(hourly_vol, 1),
        "hourly_count": round(hourly_count, 1),
        "vol_at_yes_bid": round(vol_at_yes_bid, 0),
        "vol_at_no_bid": round(vol_at_no_bid, 0),
        "taker_yes_pct": round(taker_yes_pct, 1),
        # Fees & edge
        "maker_fee": round(maker_fee, 3),
        "taker_fee": round(taker_fee, 3),
        "net_edge": round(net_edge, 3),
        # Queue wait
        "queue_wait_yes_hrs": (round(queue_wait_yes, 2)
                               if queue_wait_yes != float("inf") else "inf"),
        "queue_wait_no_hrs": (round(queue_wait_no, 2)
                              if queue_wait_no != float("inf") else "inf"),
        # Volume
        "volume_total": m["_vol"],
        "volume_24h": m["_vol24h"],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=30,
                        help="How many top markets to analyze")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    client = KalshiClient(api_key, pk_path, PROD_BASE)

    print("=" * 70)
    print("MARKET MAKING TARGET DIAGNOSTIC — Kalshi Production")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Step 1: All active markets
    print("\nStep 1: Fetching active markets...")
    markets = fetch_active_markets(client)
    print(f"  {len(markets)} active markets")

    # Filter: need some volume to be interesting
    candidates = [m for m in markets if m["_vol"] > 5000]
    print(f"  {len(candidates)} with volume > 5k")
    print(f"  Analyzing top {args.top} by 24h volume...")

    # Step 2: Analyze each
    results = []
    errors = []
    top_n = candidates[:args.top]

    for i, m in enumerate(top_n):
        ticker = m["ticker"]
        print(f"\n  [{i+1}/{len(top_n)}] {ticker}", end="", flush=True)

        analysis = analyze_market(client, m)
        if "error" in analysis:
            print(f"  SKIP ({analysis['error']})")
            errors.append(analysis)
            continue

        print(f"  spread={analysis['spread']}¢"
              f"  edge={analysis['net_edge']:.2f}¢"
              f"  vol/hr={analysis['hourly_vol']:.0f}"
              f"  queue_yes={analysis['queue_wait_yes_hrs']}"
              f"  depth_yes={analysis['yes_depth_best']:,}")

        results.append(analysis)
        time.sleep(0.2)  # gentle pacing

    save_json(results, "mm_targets_full.json")

    # Step 3: Report
    print("\n" + "=" * 70)
    print("TIER 1: IDEAL MM TARGETS")
    print("Criteria: spread >= 2¢, net edge > 0, queue wait < 2h, hourly vol > 10")
    print("=" * 70)

    tier1 = [r for r in results
             if r["spread"] >= 2
             and r["net_edge"] > 0
             and r["hourly_vol"] > 10
             and isinstance(r["queue_wait_yes_hrs"], (int, float))
             and r["queue_wait_yes_hrs"] < 2]
    tier1.sort(key=lambda r: -r["net_edge"])

    if tier1:
        for r in tier1:
            print(f"\n  {r['ticker']}")
            print(f"    \"{r['title']}\"")
            print(f"    spread={r['spread']}¢  net_edge={r['net_edge']:.2f}¢"
                  f"  maker_fee={r['maker_fee']:.2f}¢")
            print(f"    best_yes_bid={r['best_yes_bid']}¢"
                  f"  yes_ask={r['yes_ask']}¢")
            print(f"    hourly_vol={r['hourly_vol']:.0f}"
                  f"  hourly_trades={r['hourly_count']:.0f}")
            print(f"    queue_yes={r['queue_wait_yes_hrs']}h"
                  f"  depth_at_bid={r['yes_depth_best']:,}")
            print(f"    24h_vol={r['volume_24h']:,.0f}"
                  f"  total_vol={r['volume_total']:,.0f}")
    else:
        print("  None found with all criteria met!")

    # Tier 2: wider criteria
    print("\n" + "=" * 70)
    print("TIER 2: BROADER CANDIDATES")
    print("Criteria: spread >= 2¢ AND net edge > 0")
    print("=" * 70)

    tier2 = [r for r in results
             if r["spread"] >= 2
             and r["net_edge"] > 0
             and r not in tier1]
    tier2.sort(key=lambda r: -r["net_edge"])

    for r in tier2[:15]:
        qw = r["queue_wait_yes_hrs"]
        qw_str = f"{qw:.1f}h" if isinstance(qw, (int, float)) else qw
        print(f"  {r['ticker']:35s}  spread={r['spread']:2d}¢"
              f"  edge={r['net_edge']:5.2f}¢"
              f"  vol/hr={r['hourly_vol']:>8.0f}"
              f"  queue={qw_str:>8s}"
              f"  depth={r['yes_depth_best']:>8,}")

    # Tier 3: all markets for reference
    print("\n" + "=" * 70)
    print("ALL ANALYZED MARKETS (sorted by net edge)")
    print("=" * 70)

    results.sort(key=lambda r: -r.get("net_edge", -999))
    print(f"  {'Ticker':35s}  {'Sprd':>4s}  {'Edge':>6s}  {'Vol/hr':>8s}"
          f"  {'QueueY':>8s}  {'DepthY':>8s}  {'24hVol':>10s}")
    print(f"  {'─' * 85}")
    for r in results:
        qw = r["queue_wait_yes_hrs"]
        qw_str = f"{qw:.1f}h" if isinstance(qw, (int, float)) else qw
        print(f"  {r['ticker']:35s}  {r['spread']:3d}¢"
              f"  {r['net_edge']:5.2f}¢"
              f"  {r['hourly_vol']:>8.0f}"
              f"  {qw_str:>8s}"
              f"  {r['yes_depth_best']:>8,}"
              f"  {r['volume_24h']:>10,.0f}")

    save_json(tier1, "mm_tier1_targets.json")
    save_json(tier2, "mm_tier2_targets.json")

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Markets analyzed: {len(results)}")
    print(f"  Tier 1 (ideal):   {len(tier1)}")
    print(f"  Tier 2 (broader): {len(tier2)}")
    print(f"  Skipped (errors): {len(errors)}")
    print(f"  Output: {OUTPUT_DIR}/mm_targets_full.json")

    if tier1:
        best = tier1[0]
        print(f"\n  RECOMMENDATION: {best['ticker']}")
        print(f"    {best['title']}")
        print(f"    {best['spread']}¢ spread, {best['net_edge']:.2f}¢ net edge,"
              f" {best['hourly_vol']:.0f} contracts/hr")


if __name__ == "__main__":
    main()
