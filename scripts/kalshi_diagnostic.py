"""
Kalshi API Diagnostic Script.

Standalone tool that hits the Kalshi API (demo or production),
captures raw JSON responses, and produces an analysis report.

Usage:
    python scripts/kalshi_diagnostic.py              # production
    python scripts/kalshi_diagnostic.py --demo       # demo environment
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path so we can import src/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, DEMO_BASE, PROD_BASE

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
OUTPUT_DIR = Path("data/kalshi_diagnostic")


def save_json(data, filename):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved → {filepath}")


# ---------------------------------------------------------------------------
# Diagnostic Steps
# ---------------------------------------------------------------------------

def step1_top_markets_by_volume(client: KalshiClient):
    """Fetch top 20 markets by volume — what categories dominate?"""
    print("\n" + "=" * 60)
    print("STEP 1: Top 20 active markets by volume")
    print("=" * 60)

    # Fetch a large batch and sort client-side (API doesn't have sort param)
    all_markets = []
    cursor = None
    for _ in range(5):  # up to 500 markets
        data = client.get_markets(limit=100, status="open", cursor=cursor)
        batch = data.get("markets", [])
        all_markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    print(f"  Fetched {len(all_markets)} active markets total")

    # Sort by volume
    for m in all_markets:
        m["_vol"] = float(m.get("volume_fp", 0) or 0)
    top20 = sorted(all_markets, key=lambda m: m["_vol"], reverse=True)[:20]
    save_json(top20, "top20_by_volume.json")

    # Category breakdown
    categories = {}
    for m in all_markets:
        cat = m.get("category", "unknown")
        if cat not in categories:
            categories[cat] = {"count": 0, "total_vol": 0}
        categories[cat]["count"] += 1
        categories[cat]["total_vol"] += m["_vol"]

    print(f"\n  Category breakdown ({len(categories)} categories):")
    for cat, info in sorted(categories.items(),
                            key=lambda x: x[1]["total_vol"], reverse=True):
        print(f"    {cat:30s}  {info['count']:4d} markets  "
              f"vol={info['total_vol']:>12,.0f}")

    print(f"\n  Top 20 by volume:")
    for i, m in enumerate(top20, 1):
        ticker = m.get("ticker", "?")[:35]
        title = m.get("title", m.get("yes_sub_title", "?"))[:40]
        print(f"    {i:2d}. {ticker:35s}  vol={m['_vol']:>10,.0f}  "
              f"yes_bid=${m.get('yes_bid_dollars', '?')}  "
              f"yes_ask=${m.get('yes_ask_dollars', '?')}  "
              f"\"{title}\"")

    return top20, categories


def step2_orderbooks(client: KalshiClient, top_markets: list):
    """Fetch order books for top 5 most active markets — actual spreads."""
    print("\n" + "=" * 60)
    print("STEP 2: Order books for top 5 markets")
    print("=" * 60)

    books = []
    for m in top_markets[:5]:
        ticker = m["ticker"]
        title = m.get("title", m.get("yes_sub_title", "?"))[:50]
        print(f"\n  {ticker}")
        print(f"  \"{title}\"")

        try:
            data = client.get_orderbook(ticker, depth=20)
            book = data.get("orderbook", data)
        except Exception as e:
            print(f"    ERROR: {e}")
            books.append({"ticker": ticker, "error": str(e)})
            continue

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        if yes_bids and no_bids:
            best_yes_bid = yes_bids[-1][0]
            best_no_bid = no_bids[-1][0]
            implied_yes_ask = 100 - best_no_bid
            spread = implied_yes_ask - best_yes_bid
            yes_depth = sum(lvl[1] for lvl in yes_bids)
            no_depth = sum(lvl[1] for lvl in no_bids)
            print(f"    YES bids: {len(yes_bids)} levels, "
                  f"best={best_yes_bid}¢, depth={yes_depth} contracts")
            print(f"    NO bids:  {len(no_bids)} levels, "
                  f"best={best_no_bid}¢, depth={no_depth} contracts")
            print(f"    Implied YES ask: {implied_yes_ask}¢  "
                  f"Spread: {spread}¢")
        elif yes_bids:
            print(f"    YES bids only: {len(yes_bids)} levels, "
                  f"best={yes_bids[-1][0]}¢  (no NO side)")
        elif no_bids:
            print(f"    NO bids only: {len(no_bids)} levels, "
                  f"best={no_bids[-1][0]}¢  (no YES side)")
        else:
            print(f"    Empty order book")

        book["_ticker"] = ticker
        books.append(book)

    save_json(books, "orderbooks_top5.json")
    return books


def step3_resolved_markets(client: KalshiClient):
    """Fetch 10 recently settled markets — verify resolution format."""
    print("\n" + "=" * 60)
    print("STEP 3: 10 recently settled markets")
    print("=" * 60)

    data = client.get_markets(limit=10, status="settled")
    markets = data.get("markets", [])
    save_json(markets, "resolved_markets.json")

    print(f"\n  Got {len(markets)} resolved markets")
    for m in markets:
        ticker = m.get("ticker", "?")[:35]
        title = m.get("title", m.get("yes_sub_title", "?"))[:40]
        print(f"    {ticker:35s}  result={m.get('result')!r:6s}  "
              f"settle=${m.get('settlement_value_dollars', '?')}  "
              f"last=${m.get('last_price_dollars', '?')}  "
              f"\"{title}\"")

    return markets


def step4_fee_check(client: KalshiClient):
    """Check fee structure by examining market details."""
    print("\n" + "=" * 60)
    print("STEP 4: Fee structure check")
    print("=" * 60)

    # Check balance endpoint (shows fee tier info)
    try:
        balance = client.get_balance()
        print(f"\n  Balance response: {json.dumps(balance, indent=4)}")
        save_json(balance, "balance.json")
    except Exception as e:
        print(f"  Balance error: {e}")

    # Check API limits (may show fee tier)
    try:
        limits = client.get("/account/api_limits")
        print(f"\n  API limits: {json.dumps(limits, indent=4)}")
        save_json(limits, "api_limits.json")
    except Exception as e:
        print(f"  API limits error: {e}")

    # Check series fee info
    try:
        series = client.get("/series/fees")
        print(f"\n  Series fees: {json.dumps(series, indent=4)[:500]}")
        save_json(series, "fees.json")
    except Exception as e:
        print(f"  Series fees error: {e}")

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kalshi API diagnostic")
    parser.add_argument("--demo", action="store_true",
                        help="Use demo environment instead of production")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    base_url = DEMO_BASE if args.demo else PROD_BASE
    env_name = "DEMO" if args.demo else "PRODUCTION"

    print("Kalshi API Diagnostic")
    print(f"Environment: {env_name} ({base_url})")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"API Key ID: {api_key[:8]}...")

    client = KalshiClient(api_key, pk_path, base_url)

    results = {}
    try:
        top20, categories = step1_top_markets_by_volume(client)
        results["top20"] = top20
        results["categories"] = categories

        results["orderbooks"] = step2_orderbooks(client, top20)
        results["resolved"] = step3_resolved_markets(client)
        step4_fee_check(client)
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    save_json(results, "sample_markets.json")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Active markets scanned: {len(results.get('top20', []))}")
    print(f"  Categories found: {len(results.get('categories', {}))}")
    print(f"  Order books fetched: {len(results.get('orderbooks', []))}")
    print(f"  Resolved markets: {len(results.get('resolved', []))}")
    print(f"\n  All raw JSON saved to: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
