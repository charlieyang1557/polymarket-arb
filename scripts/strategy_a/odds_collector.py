#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Odds Comparison Collector: Pinnacle vs Polymarket US.

Fetches Pinnacle odds from The-Odds-API and Polymarket US prices,
matches events, computes delta, and stores in SQLite.

Designed to run via crontab twice daily:
  0 16,22 * * * cd ~/polymarket-arb && python3 -u scripts/strategy_a/odds_collector.py >> data/strategy_a/collector.log 2>&1

Requires ODDS_API_KEY in .env (The-Odds-API free tier).

Usage:
    python3 scripts/strategy_a/odds_collector.py
    python3 scripts/strategy_a/odds_collector.py --dry-run
    python3 scripts/strategy_a/odds_collector.py --sports basketball_nba
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from src.strategy_a.devig import devig_decimal
from src.strategy_a.matching import (
    match_event_to_slugs, normalize_team, SPORT_TO_SLUG,
)
from src.strategy_a.odds_db import OddsDB

# The-Odds-API configuration
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_SPORTS = [
    "basketball_nba",
    "icehockey_nhl",
    "baseball_mlb",
    "americanfootball_nfl",
    "basketball_ncaab",
    "mma_mixed_martial_arts",
]
# Bookmakers to request (pinnacle = sharp line, others for comparison)
BOOKMAKERS = "pinnacle,fanduel,draftkings"

DB_PATH = "data/strategy_a/odds_comparison.db"


def fetch_odds_api(sport: str, api_key: str) -> list[dict]:
    """Fetch upcoming odds from The-Odds-API for one sport.

    Returns list of event dicts with bookmaker odds.
    Each API call costs 1 credit per sport.
    """
    import requests

    url = f"{ODDS_API_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us,eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "bookmakers": BOOKMAKERS,
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()

    # Track credit usage from headers
    remaining = resp.headers.get("x-requests-remaining", "?")
    used = resp.headers.get("x-requests-used", "?")
    print(f"    API credits: {used} used, {remaining} remaining", flush=True)

    return resp.json()


def extract_pinnacle_odds(event: dict) -> dict | None:
    """Extract Pinnacle h2h odds from an event.

    Returns {home_odds, away_odds, home_team, away_team} or None.
    """
    for bm in event.get("bookmakers", []):
        if bm.get("key") != "pinnacle":
            continue

        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue

            outcomes = market.get("outcomes", [])
            if len(outcomes) != 2:
                continue

            # outcomes[0] = home, outcomes[1] = away (by convention)
            home_name = outcomes[0].get("name", "")
            away_name = outcomes[1].get("name", "")
            home_odds = float(outcomes[0].get("price", 0))
            away_odds = float(outcomes[1].get("price", 0))

            if home_odds <= 1.0 or away_odds <= 1.0:
                continue

            return {
                "home_team": home_name,
                "away_team": away_name,
                "home_odds": home_odds,
                "away_odds": away_odds,
            }

    return None


def fetch_polymarket_prices(client, slugs: list[str]) -> dict:
    """Fetch current BBO for Polymarket slugs.

    Returns {slug: {yes_price, no_price, spread}}.
    """
    prices = {}
    for slug in slugs:
        try:
            bbo = client.get_bbo(slug)
            bid = bbo.get("best_bid_cents", 0)
            ask = bbo.get("best_ask_cents", 0)
            if bid > 0 and ask > 0:
                prices[slug] = {
                    "yes_price": bid / 100,  # best bid = YES price
                    "no_price": (100 - ask) / 100,  # complement
                    "spread": ask - bid,
                }
            time.sleep(0.05)
        except Exception as e:
            print(f"    BBO error {slug}: {e}", flush=True)

    return prices


def fetch_polymarket_slugs(client) -> list[str]:
    """Fetch all active Polymarket US market slugs."""
    all_slugs = []
    offset = 0
    while offset < 2000:
        resp = client.client.markets.list({
            "limit": 100, "offset": offset,
            "active": True, "closed": False,
        })
        batch = resp.get("markets", [])
        if not batch:
            break
        for m in batch:
            slug = m.get("slug", "")
            if slug:
                all_slugs.append(slug)
        offset += len(batch)
        if len(batch) < 100:
            break
    return all_slugs


def run_collection(sports: list[str], api_key: str, dry_run: bool = False,
                   db_path: str = DB_PATH, verbose: bool = False):
    """Main collection loop."""
    now = datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    print(f"\n{'='*60}")
    print(f"Odds Collector — {ts}")
    print(f"{'='*60}")

    # Step 1: Fetch odds from The-Odds-API
    all_events = []
    for sport in sports:
        print(f"\n  Fetching {sport}...", flush=True)
        if dry_run:
            print(f"    [DRY-RUN] Would fetch {sport} from Odds-API")
            continue
        try:
            events = fetch_odds_api(sport, api_key)
            for e in events:
                e["_sport"] = sport
            all_events.extend(events)
            print(f"    Got {len(events)} events", flush=True)
        except Exception as e:
            print(f"    ERROR: {e}", flush=True)
        time.sleep(0.2)

    if dry_run:
        print(f"\n  [DRY-RUN] Would fetch Polymarket slugs and match")
        return

    # Step 2: Extract Pinnacle odds
    pinnacle_events = []
    for event in all_events:
        pin = extract_pinnacle_odds(event)
        if pin is None:
            continue
        pin["sport"] = event.get("_sport", "")
        pin["commence_time"] = event.get("commence_time", "")
        pin["event_id"] = event.get("id", "")
        pinnacle_events.append(pin)

    print(f"\n  Pinnacle events: {len(pinnacle_events)}")

    # Step 3: Fetch Polymarket slugs
    print(f"  Fetching Polymarket US markets...", flush=True)
    from src.poly_client import PolyClient
    poly = PolyClient()
    slugs = fetch_polymarket_slugs(poly)
    print(f"  Polymarket active slugs: {len(slugs)}")

    # Step 4: Match and compute deltas
    db = OddsDB(db_path)
    matched = 0
    unmatched = []
    snapshots = []

    for pin in pinnacle_events:
        slug = match_event_to_slugs(
            home_team=pin["home_team"],
            away_team=pin["away_team"],
            sport=pin["sport"],
            commence_time=pin["commence_time"],
            slugs=slugs,
            verbose=verbose,
        )

        if slug is None:
            unmatched.append(
                f"{pin['home_team']} vs {pin['away_team']} "
                f"[{pin['sport']}]")
            continue

        # Fetch Polymarket BBO
        try:
            bbo = poly.get_bbo(slug)
            bid = bbo.get("best_bid_cents", 0)
            ask = bbo.get("best_ask_cents", 0)
        except Exception:
            continue

        if bid <= 0 or ask <= 0:
            continue

        yes_price = bid / 100
        no_price = 1 - ask / 100
        spread = ask - bid

        # De-vig Pinnacle
        try:
            home_prob, away_prob, vig = devig_decimal(
                pin["home_odds"], pin["away_odds"])
        except ValueError:
            continue

        # Hours to game
        hours_to_game = 0
        try:
            game_dt = datetime.fromisoformat(
                pin["commence_time"].replace("Z", "+00:00"))
            hours_to_game = (game_dt - now).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

        delta_home = round(home_prob - yes_price, 4)
        delta_away = round(away_prob - no_price, 4)

        event_str = f"{pin['home_team']} vs {pin['away_team']}"

        snapshot = {
            "timestamp": ts,
            "slug": slug,
            "sport": pin["sport"],
            "event": event_str,
            "game_start": pin["commence_time"],
            "hours_to_game": round(hours_to_game, 1),
            "pinnacle_home_prob": home_prob,
            "pinnacle_away_prob": away_prob,
            "pinnacle_raw_home": pin["home_odds"],
            "pinnacle_raw_away": pin["away_odds"],
            "pinnacle_vig": vig,
            "poly_yes_price": yes_price,
            "poly_no_price": no_price,
            "poly_spread": spread,
            "delta_home": delta_home,
            "delta_away": delta_away,
            "market_type": "moneyline",
        }

        db.insert_snapshot(snapshot)
        snapshots.append(snapshot)
        matched += 1

        # Print each matched pair
        direction = "Pin>Poly" if delta_home > 0 else "Poly>Pin"
        print(f"    {event_str[:40]:40s} Pin={home_prob:.1%}/{away_prob:.1%} "
              f"Poly={yes_price:.0%}/{1-yes_price:.0%} "
              f"Δ={delta_home:+.1%} ({direction})", flush=True)

    db.close()

    # Step 5: Summary
    print(f"\n  SUMMARY:")
    print(f"    Pinnacle events:    {len(pinnacle_events)}")
    print(f"    Polymarket slugs:   {len(slugs)}")
    print(f"    Matched:            {matched}")
    print(f"    Unmatched:          {len(unmatched)}")

    if snapshots:
        deltas = [abs(s["delta_home"]) for s in snapshots]
        avg_delta = sum(deltas) / len(deltas)
        max_delta = max(deltas)
        max_event = max(snapshots, key=lambda s: abs(s["delta_home"]))
        big = sum(1 for d in deltas if d > 0.03)

        print(f"    Avg |delta|:        {avg_delta:.1%}")
        print(f"    Max |delta|:        {max_delta:.1%} "
              f"({max_event['event'][:40]})")
        print(f"    |delta| > 3%:       {big} ({100*big/len(deltas):.0f}%)")

        # Per-sport breakdown
        from collections import defaultdict
        by_sport = defaultdict(list)
        for s in snapshots:
            by_sport[s["sport"]].append(s)
        if len(by_sport) > 1:
            print(f"\n  Per-sport:")
            for sport, rows in sorted(by_sport.items(),
                                       key=lambda x: -len(x[1])):
                d = [abs(r["delta_home"]) for r in rows]
                avg = sum(d) / len(d)
                print(f"    {sport:<30s}: {len(rows):3d} matched, "
                      f"avg |Δ|={avg:.1%}")

    if unmatched:
        print(f"\n  Unmatched events (first 10):")
        for u in unmatched[:10]:
            print(f"    {u}")


def main():
    parser = argparse.ArgumentParser(
        description="Odds Comparison Collector: Pinnacle vs Polymarket US")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without API calls")
    parser.add_argument("--sports", nargs="+", default=DEFAULT_SPORTS,
                        help="Sports to check (default: all major)")
    parser.add_argument("--db", default=DB_PATH,
                        help="SQLite database path")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed matching debug output")
    args = parser.parse_args()

    db_path = args.db

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key and not args.dry_run:
        print("FATAL: ODDS_API_KEY not set in .env", file=sys.stderr)
        print("Sign up at https://the-odds-api.com for free tier (500 req/mo)")
        sys.exit(1)

    run_collection(args.sports, api_key or "", dry_run=args.dry_run,
                   db_path=db_path, verbose=args.verbose)


if __name__ == "__main__":
    main()
