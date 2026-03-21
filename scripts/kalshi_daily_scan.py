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
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, PROD_BASE
from src.mm.engine import discord_notify

# Only allow sports where our live-game detection (>50 trades/5min) works.
# E-sports have too little volume — bot can't detect game start.
ALLOWED_SPORT_PREFIXES = ("KXNBA", "KXNCAAMB", "KXNCAAWB", "KXNHL",
                          "KXMLB", "KXWBC", "KXNCAAFB")


def is_allowed_sport(ticker: str) -> bool:
    """Check if ticker belongs to an allowed sport."""
    return any(ticker.startswith(p) for p in ALLOWED_SPORT_PREFIXES)

load_dotenv()
OUTPUT_DIR = Path("data/kalshi_diagnostic")


def net_spread_cents(spread: int, midpoint: float) -> int:
    """Net spread after maker fees on both legs.

    maker_fee per side = ceil(0.0175 * P * (1-P) * 100) in cents.
    net_spread = spread - 2 * maker_fee.
    """
    p = midpoint / 100
    fee_per_side = math.ceil(0.0175 * p * (1 - p) * 100)
    return spread - 2 * fee_per_side


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Rank-based composite scoring for passing candidates.

    Ranks each metric independently (average rank for ties),
    then averages the three ranks. Lower composite = better.
    """
    passing = [c for c in candidates if c.get("passes")]
    failing = [c for c in candidates if not c.get("passes")]

    if not passing:
        return failing + passing

    # Rank with average ties
    def avg_rank(values, ascending=True):
        """Return average ranks. ascending=True means lowest value = rank 1."""
        indexed = sorted(enumerate(values),
                         key=lambda x: x[1] if ascending else -x[1])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(indexed):
            # Find tie group
            j = i
            while j < len(indexed) and indexed[j][1] == indexed[i][1]:
                j += 1
            avg_r = sum(range(i + 1, j + 1)) / (j - i)
            for k in range(i, j):
                ranks[indexed[k][0]] = avg_r
            i = j
        return ranks

    net_spreads = [c["net_spread"] for c in passing]
    queues = [c["binding_queue"] for c in passing]
    freqs = [c["trades_per_hour"] for c in passing]

    spread_ranks = avg_rank(net_spreads, ascending=False)  # highest = rank 1
    queue_ranks = avg_rank(queues, ascending=True)          # lowest = rank 1
    freq_ranks = avg_rank(freqs, ascending=False)           # highest = rank 1

    for i, c in enumerate(passing):
        c["rank_spread"] = spread_ranks[i]
        c["rank_queue"] = queue_ranks[i]
        c["rank_freq"] = freq_ranks[i]
        c["composite_rank"] = round(
            (spread_ranks[i] + queue_ranks[i] + freq_ranks[i]) / 3, 2)

    # Sort passing by composite (lowest = best)
    passing.sort(key=lambda c: c["composite_rank"])

    # TODO: After 100+ fills, consider merging queue and
    # frequency into a single "Expected Time to Fill" metric:
    # ETF = max_queue_depth / (trades_per_minute)
    # This captures the physical relationship between these
    # two dimensions. Need actual fill data to calibrate.

    return passing + failing


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

                # Only allowed sports (no e-sports — live detection fails)
                if not is_allowed_sport(ticker):
                    continue

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
    """Fetch orderbooks and trades, apply pre-filters."""
    from scripts.kalshi_mm_scanner import _parse_book_levels

    now = datetime.now(timezone.utc)
    checked = []
    for c in candidates[:max_check]:
        ticker = c["ticker"]
        try:
            data = client.get_orderbook(ticker, depth=20)
            yes_levels, no_levels = _parse_book_levels(data)

            yes_depth = sum(s for _, s in yes_levels) if yes_levels else 0
            no_depth = sum(s for _, s in no_levels) if no_levels else 0

            # L1 (best level) queue depth
            yes_best_depth = yes_levels[0][1] if yes_levels else 0
            no_best_depth = no_levels[0][1] if no_levels else 0

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
            c["yes_best_depth"] = yes_best_depth
            c["no_best_depth"] = no_best_depth

            # Binding queue = max(yes, no) — round-trip speed = slowest leg
            c["binding_queue"] = max(yes_depth, no_depth)

            # Net spread after fees
            c["net_spread"] = net_spread_cents(c["spread"], c["midpoint"])

            # Fetch trade frequency
            try:
                trade_data = client.get_trades(ticker, limit=200)
                trades = trade_data.get("trades", [])
                cutoff_1h = now - timedelta(hours=1)
                recent = []
                for t in trades:
                    try:
                        ts = datetime.fromisoformat(
                            t["created_time"].replace("Z", "+00:00"))
                        if ts >= cutoff_1h:
                            recent.append(ts)
                    except (KeyError, ValueError):
                        continue
                if len(recent) >= 2:
                    span_h = max(
                        (max(recent) - min(recent)).total_seconds() / 3600,
                        0.01)
                    c["trades_per_hour"] = round(len(recent) / span_h, 1)
                else:
                    c["trades_per_hour"] = float(len(recent))
            except Exception:
                c["trades_per_hour"] = 0.0

            # Time to expiration filter
            hours_to_exp = 999
            exp = c.get("expected_expiration", "")
            if exp:
                try:
                    exp_dt = datetime.fromisoformat(
                        exp.replace("Z", "+00:00"))
                    hours_to_exp = (exp_dt - now).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass
            c["hours_to_exp"] = round(hours_to_exp, 1)

            # Pre-filters (binary)
            max_best_depth = max(yes_best_depth, no_best_depth)
            c["max_best_depth"] = max_best_depth
            c["passes"] = (c["net_spread"] >= 2
                           and c["net_spread"] <= 8
                           and c["spread"] < 15
                           and 35 <= c["midpoint"] <= 65
                           and yes_best_depth > 0
                           and no_best_depth > 0
                           and 0.2 <= sym <= 5.0
                           and max_best_depth < 20000
                           and c["trades_per_hour"] >= 10
                           and hours_to_exp > 1)
            checked.append(c)

        except Exception as e:
            c["symmetry"] = 0.0
            c["trades_per_hour"] = 0.0
            c["net_spread"] = 0
            c["binding_queue"] = 0
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
    parser.add_argument("--duration", type=int, default=86400,
                        help="Paper MM duration in seconds (default: 24h)")
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

    # Phase 2: Deep check (orderbook + trades + filters)
    print("\n  Checking orderbooks + trade frequency...")
    checked = deep_check(client, candidates)

    # Phase 3: Rank passing candidates
    ranked = rank_candidates(checked)

    passing = [c for c in ranked if c.get("passes")]
    print(f"\n  Passing filters (net_spread>0, sprd<15, sym 0.2-5.0, "
          f"queue<20K, freq>=10/hr, exp>1h): {len(passing)}")
    print()

    # Table header
    header = (f"{'#':>2} {'Pass':>4} {'Ticker':<40} {'Sprd':>4} {'Net':>4} "
              f"{'Sym':>5} {'L1Q':>6} {'TotQ':>6} {'Trd/h':>6} {'Exp':>5} "
              f"{'Rank':>5}")
    print(header)
    print("-" * len(header))

    for i, c in enumerate(ranked, 1):
        flag = " OK " if c.get("passes") else "FAIL"
        sym = c.get("symmetry", 0)
        sym_s = f"{sym:.2f}" if sym < 100 else ">100"
        tph = c.get("trades_per_hour", 0)
        net = c.get("net_spread", 0)
        l1q = c.get("max_best_depth", 0)
        totq = c.get("binding_queue", 0)
        exp_h = c.get("hours_to_exp", 0)
        rank_s = f"{c['composite_rank']:.1f}" if "composite_rank" in c else "-"
        print(f"{i:2d} {flag} {c['ticker']:<40} "
              f"{c['spread']:4d} {net:4d} {sym_s:>5} {l1q:6d} {totq:6d} "
              f"{tph:6.0f} {exp_h:5.1f} {rank_s:>5}")

    # Show rank detail for passing markets
    if passing:
        print(f"\n  Rank detail (passing):")
        for c in passing:
            print(f"    {c['ticker']:<40} "
                  f"rk_sprd={c['rank_spread']:.1f} "
                  f"rk_queue={c['rank_queue']:.1f} "
                  f"rk_freq={c['rank_freq']:.1f} → "
                  f"composite={c['composite_rank']:.2f}")

    # Save targets — merge with existing, dedup by ticker
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = passing[:args.max_markets]
    target_file = OUTPUT_DIR / "daily_targets.json"

    existing = []
    if target_file.exists():
        try:
            with open(target_file) as f:
                existing = json.load(f)
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
        discord_notify("**Scanner** 0 markets pass filters — no bot launched")
        return

    print(f"\n  Selected targets:")
    for t in targets:
        print(f"    {t['ticker']} — {t['title']}")

    # Discord summary
    lines = [f"**Scanner** {len(passing)} pass / {len(checked)} checked"]
    for t in targets:
        net = t.get('net_spread', 0)
        tph = t.get('trades_per_hour', 0)
        lines.append(f"  `{t['ticker']}` net={net}c freq={tph:.0f}/hr")

    # Auto-launch paper MM if requested
    if args.run and targets:
        ticker_str = ",".join(t["ticker"] for t in targets)
        cmd = (f"nohup {sys.executable} -u scripts/paper_mm.py "
               f"--tickers {ticker_str} "
               f"--duration {args.duration} --size 2 --interval 10 "
               f"> data/mm_paper_run.log 2>&1 &")
        print(f"\n  Launching paper MM...")
        print(f"  {cmd}")
        os.system(cmd)
        print("  Paper MM started in background. Monitor: tail -f data/mm_paper_run.log")
        lines.append("Bot launched ✅")
    elif targets:
        ticker_str = ",".join(t["ticker"] for t in targets)
        print(f"\n  To run paper MM manually:")
        print(f"  python scripts/paper_mm.py --tickers {ticker_str} --duration 86400")
        lines.append("Scan only — no bot launched")

    discord_notify("\n".join(lines))


if __name__ == "__main__":
    main()
