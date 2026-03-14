"""
Kalshi Daily Sports MM Target Scanner.

Scans today's sports markets and picks the best MM candidates.
Run each morning ~9 AM ET when daily game markets are listed.

Usage:
    python scripts/kalshi_daily_scan.py              # scan + print targets
    python scripts/kalshi_daily_scan.py --run        # scan + start paper_mm.py
    python scripts/kalshi_daily_scan.py --run --max-markets 3
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
from src.kalshi_client import KalshiClient, PROD_BASE

load_dotenv()
OUTPUT_DIR = Path("data/kalshi_diagnostic")


def scan_today_sports(client: KalshiClient) -> list[dict]:
    """Find today's sports spread/total markets suitable for MM."""
    now = datetime.now(timezone.utc)
    # Today and tomorrow in UTC (covers ET evening games)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"Scanning for sports markets expiring {today_str} or {tomorrow_str}")

    candidates = []
    cursor = None
    total_events = 0

    for _ in range(50):
        data = client.get_events(limit=100, with_nested_markets=True,
                                 status="open", cursor=cursor)
        batch = data.get("events", [])
        total_events += len(batch)

        for ev in batch:
            if ev.get("category") != "Sports":
                continue

            for m in ev.get("markets", []):
                ticker = m.get("ticker", "")

                # Only spread and total markets (symmetric liquidity)
                if "SPREAD" not in ticker and "TOTAL" not in ticker:
                    continue

                exp = m.get("expected_expiration_time", "")
                if not exp:
                    continue

                # Must expire today or tomorrow
                exp_date = exp[:10]
                if exp_date != today_str and exp_date != tomorrow_str:
                    continue

                # Must expire in the future
                try:
                    exp_dt = datetime.fromisoformat(
                        exp.replace("Z", "+00:00"))
                    if exp_dt < now:
                        continue
                except (ValueError, TypeError):
                    continue

                # Parse prices
                yb_str = m.get("yes_bid_dollars", "0") or "0"
                ya_str = m.get("yes_ask_dollars", "0") or "0"
                yb = int(round(float(yb_str) * 100))
                ya = int(round(float(ya_str) * 100))

                if yb <= 0 or ya <= 0:
                    continue

                spread = ya - yb
                if spread < 2:
                    continue

                vol_24h = int(float(m.get("volume_24h_fp", "0") or "0"))
                vol_total = int(float(m.get("volume_fp", "0") or "0"))

                candidates.append({
                    "ticker": ticker,
                    "title": (m.get("title") or
                              m.get("yes_sub_title") or "")[:70],
                    "event": ev.get("title", "")[:50],
                    "category": "Sports",
                    "spread": spread,
                    "midpoint": (yb + ya) / 2,
                    "volume_24h": vol_24h,
                    "volume_total": vol_total,
                    "expected_expiration": exp,
                })

        cursor = data.get("cursor")
        time.sleep(0.1)
        if not cursor or not batch:
            break

    print(f"  Scanned {total_events} events")

    # Sort by volume, then spread
    candidates.sort(key=lambda c: (c["volume_24h"], c["spread"]),
                    reverse=True)
    return candidates


def deep_check(client: KalshiClient, candidates: list[dict],
               max_check: int = 20) -> list[dict]:
    """Fetch orderbooks to check symmetry for top candidates."""
    from scripts.kalshi_mm_scanner import _parse_book_levels

    checked = []
    for c in candidates[:max_check]:
        ticker = c["ticker"]
        try:
            data = client.get_orderbook(ticker, depth=20)
            yes_levels, no_levels = _parse_book_levels(data)

            yes_depth = sum(s for _, s in yes_levels) if yes_levels else 0
            no_depth = sum(s for _, s in no_levels) if no_levels else 0

            if yes_depth > 0 and no_depth > 0:
                sym = yes_depth / no_depth
            elif yes_depth > 0:
                sym = 999.0
            elif no_depth > 0:
                sym = 0.001
            else:
                sym = 0.0

            c["symmetry"] = round(sym, 3)
            c["yes_depth"] = yes_depth
            c["no_depth"] = no_depth
            c["passes"] = (0.2 <= sym <= 5.0 and c["spread"] >= 3)
            checked.append(c)

        except Exception as e:
            c["symmetry"] = 0.0
            c["passes"] = False
            c["error"] = str(e)
            checked.append(c)

        time.sleep(0.1)

    return checked


def main():
    parser = argparse.ArgumentParser(
        description="Daily sports MM target scanner")
    parser.add_argument("--run", action="store_true",
                        help="Auto-start paper_mm.py with selected targets")
    parser.add_argument("--max-markets", type=int, default=3,
                        help="Max markets to trade (default: 3)")
    parser.add_argument("--duration", type=int, default=43200,
                        help="Paper MM duration in seconds (default: 12h)")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    client = KalshiClient(api_key, pk_path, PROD_BASE)

    print("=" * 60)
    print("Kalshi Daily Sports MM Scanner")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    # Phase 1: Find today's candidates
    candidates = scan_today_sports(client)
    print(f"\n  Found {len(candidates)} spread/total markets today")

    if not candidates:
        print("\n  No sports spread/total markets found for today.")
        print("  Games may not be listed yet (check after 9 AM ET).")
        return

    # Phase 2: Check orderbook symmetry
    print("\n  Checking orderbook symmetry...")
    checked = deep_check(client, candidates)

    # Show results
    passing = [c for c in checked if c.get("passes")]
    print(f"\n  Passing filters (spread >= 3c, sym 0.2-5.0): {len(passing)}")
    print()

    header = f"{'#':>2} {'Pass':>4} {'Ticker':<45} {'Sprd':>4} {'Sym':>5} {'Vol':>7}"
    print(header)
    print("-" * len(header))

    for i, c in enumerate(checked, 1):
        flag = " OK " if c.get("passes") else "FAIL"
        sym = c.get("symmetry", 0)
        sym_s = f"{sym:.2f}" if sym < 100 else ">100"
        print(f"{i:2d} {flag} {c['ticker']:<45} "
              f"{c['spread']:4d} {sym_s:>5} {c['volume_24h']:7d}")

    # Save targets — merge with existing, dedup by ticker
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = passing[:args.max_markets]
    target_file = OUTPUT_DIR / "daily_targets.json"

    existing = []
    if target_file.exists():
        try:
            existing = json.load(open(target_file))
        except (json.JSONDecodeError, IOError):
            existing = []

    existing_tickers = {t["ticker"] for t in existing}
    merged = list(existing)
    added = 0
    for t in targets:
        if t["ticker"] not in existing_tickers:
            merged.append(t)
            existing_tickers.add(t["ticker"])
            added += 1

    with open(target_file, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"\n  Targets: {len(existing)} existing + {added} new = {len(merged)} total")
    print(f"  Saved to {target_file}")

    # Also write a simple ticker list
    ticker_file = OUTPUT_DIR / "daily_targets.txt"
    tickers = [t["ticker"] for t in merged]
    with open(ticker_file, "w") as f:
        f.write(",".join(tickers))
    print(f"  Ticker list: {ticker_file}")

    if not targets:
        print("\n  No markets pass all filters. Try again later.")
        return

    print(f"\n  Selected targets:")
    for t in targets:
        print(f"    {t['ticker']} — {t['title']}")

    # Auto-launch paper MM if requested
    if args.run and targets:
        ticker_str = ",".join(t["ticker"] for t in targets)
        cmd = (f"nohup python -u scripts/paper_mm.py "
               f"--tickers {ticker_str} "
               f"--duration {args.duration} --size 2 --interval 10 "
               f"> data/mm_paper_run.log 2>&1 &")
        print(f"\n  Launching paper MM...")
        print(f"  {cmd}")
        os.system(cmd)
        print("  Paper MM started in background. Monitor: tail -f data/mm_paper_run.log")
    elif targets:
        ticker_str = ",".join(t["ticker"] for t in targets)
        print(f"\n  To run paper MM manually:")
        print(f"  python scripts/paper_mm.py --tickers {ticker_str} --duration 43200")


if __name__ == "__main__":
    main()
