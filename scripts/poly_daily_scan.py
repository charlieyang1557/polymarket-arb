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
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.poly_client import PolyClient, normalize_orderbook, calculate_maker_fee

OUTPUT_DIR = Path("data/polymarket_diagnostic")

# Rebate config (sports default for Polymarket US)
TAKER_FEE_PCT = 0.02
REBATE_PCT = 0.25

SCHEDULE_PATH = "data/game_schedule.json"
SCHEDULE_MAX_AGE_HOURS = 6

# Slug date pattern: YYYY-MM-DD somewhere in the slug
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Known sport codes in Polymarket slugs
_SPORTS = {"nba", "nhl", "mlb", "nfl", "cbb", "wcbb", "cfb",
           "ufc", "atp", "wta", "epl", "ucl", "mls", "sea",
           "bun", "lal", "masters"}


# ---------------------------------------------------------------------------
# Slug parsing + schedule matching (tested)
# ---------------------------------------------------------------------------

def parse_slug(slug: str) -> dict:
    """Parse a Polymarket slug into sport, teams, date.

    Slug format: prefix-sport-team1-team2-YYYY-MM-DD[-detail]
    Examples:
      tsc-nba-sac-atl-2026-03-28-238pt5
      asc-nba-sac-atl-2026-03-28-pos-15pt5
      aec-mlb-pit-nym-2026-03-28
      aec-cbb-bayl-minnst-2026-04-01
      tec-nba-champ-2026-07-01-okc
    """
    result = {"sport": None, "team1": None, "team2": None, "date": None}

    # Extract date
    date_match = _DATE_RE.search(slug)
    if date_match:
        result["date"] = date_match.group(1)

    parts = slug.split("-")
    if len(parts) < 4:
        return result

    # Parts[0] = prefix (tsc/asc/aec/tec/atc), parts[1] = sport
    sport = parts[1].lower()
    if sport in _SPORTS:
        result["sport"] = sport
    else:
        return result

    # Teams: parts after sport, before date
    # Find where the date starts in parts
    date_str = result["date"]
    if date_str:
        date_parts = date_str.split("-")  # ["2026", "03", "28"]
        try:
            date_idx = parts.index(date_parts[0])
        except ValueError:
            date_idx = len(parts)
    else:
        date_idx = len(parts)

    # Team parts are between sport (idx 1) and date start
    team_parts = parts[2:date_idx]
    if len(team_parts) >= 2:
        result["team1"] = team_parts[0].lower()
        result["team2"] = team_parts[1].lower()
    elif len(team_parts) == 1:
        result["team1"] = team_parts[0].lower()

    return result


def match_slug_to_schedule(slug: str, schedule_games: list[dict]) -> str | None:
    """Match a Polymarket slug to a game in the schedule.

    Returns game_start_utc string if matched, None otherwise.

    Matching logic:
      1. Parse slug → sport, team1, team2, date
      2. For each game in schedule:
         - Sport must match (case-insensitive)
         - Both teams must match (either order, case-insensitive)
         - Date within ±1 day (games near midnight cross dates)
    """
    parsed = parse_slug(slug)
    if not parsed["sport"] or not parsed["team1"]:
        return None

    slug_sport = parsed["sport"].lower()
    slug_t1 = parsed["team1"].lower()
    slug_t2 = (parsed["team2"] or "").lower()
    slug_date = parsed["date"]

    for game in schedule_games:
        game_sport = (game.get("sport") or "").lower()
        away = (game.get("away_team") or "").lower()
        home = (game.get("home_team") or "").lower()
        start = game.get("start_time_utc", "")

        if not start:
            continue

        # Sport check
        if game_sport != slug_sport:
            continue

        # Team check: both must match in either order
        slug_teams = {slug_t1, slug_t2} - {""}
        game_teams = {away, home} - {""}
        if not slug_teams or not game_teams:
            continue
        if not slug_teams.issubset(game_teams):
            continue

        # Date check: within ±1 day
        if slug_date:
            game_date = start[:10]  # "2026-03-28"
            try:
                sd = datetime.strptime(slug_date, "%Y-%m-%d").date()
                gd = datetime.strptime(game_date, "%Y-%m-%d").date()
                if abs((sd - gd).days) > 1:
                    continue
            except ValueError:
                pass

        return start

    return None


def extract_game_start_from_market(market: dict) -> str | None:
    """Extract game start time from a market dict.

    Priority:
      1. gameStartTime (on market from SDK)
      2. _event_start_time (injected from event-level startTime)

    Returns ISO string or None.
    """
    gst = market.get("gameStartTime") or ""
    if gst:
        return gst

    est = market.get("_event_start_time") or ""
    if est:
        return est

    return None


def load_game_schedule(path: str = SCHEDULE_PATH) -> list[dict]:
    """Load game schedule, return games list. Empty list if missing/stale."""
    try:
        with open(path) as f:
            data = json.load(f)

        updated_at = data.get("updated_at")
        if updated_at:
            updated = datetime.fromisoformat(
                updated_at.replace("Z", "+00:00"))
            age_hours = ((datetime.now(timezone.utc) - updated).total_seconds()
                         / 3600)
            if age_hours > SCHEDULE_MAX_AGE_HOURS:
                print(f"  WARNING: game_schedule.json is {age_hours:.1f}h old — "
                      "treating as stale")
                return []

        return data.get("games", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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


def filter_by_hours_to_game(candidates: list[dict], max_hours: int = 18,
                            now: datetime | None = None) -> list[dict]:
    """Exclude passing candidates where game_start is > max_hours away.

    Distant markets have mirage liquidity (wide spreads that disappear
    as game approaches). Only trade today's games.

    Non-passing candidates are kept (they're already excluded from ranking).
    Candidates without game_start_time are kept (can't filter).
    """
    now = now or datetime.now(timezone.utc)
    result = []
    for c in candidates:
        if not c.get("passes"):
            result.append(c)
            continue

        gst = c.get("game_start_time") or ""
        if not gst:
            result.append(c)
            continue

        try:
            start = datetime.fromisoformat(gst.replace("Z", "+00:00"))
            hours = (start - now).total_seconds() / 3600
            if hours > max_hours:
                c["passes"] = False
                c["skip_reason"] = f"game in {hours:.0f}h (>{max_hours}h)"
                print(f"    SKIP: {c['slug']} game in {hours:.0f}h", flush=True)
            # Also skip if game already started
            elif hours < 0:
                c["passes"] = False
                c["skip_reason"] = "game already started"
        except (ValueError, TypeError):
            pass

        result.append(c)
    return result


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

    # Attach game start times
    # Priority 1: SDK gameStartTime (already in candidate)
    # Priority 2: game_schedule.json fallback
    schedule_games = load_game_schedule()
    if schedule_games:
        print(f"\n  Game schedule: {len(schedule_games)} games loaded")

    matched = 0
    for c in candidates:
        gst = c.get("game_start_time") or ""
        if not gst and schedule_games:
            gst = match_slug_to_schedule(c["slug"], schedule_games) or ""
            if gst:
                c["game_start_time"] = gst
        if gst:
            matched += 1
    print(f"  Game start times: {matched}/{len(candidates)} markets matched")

    # Quick stats
    spreads = [c["spread"] for c in candidates]
    print(f"\n  Spread distribution:")
    for lo, hi, label in [(0, 2, "0-1c"), (2, 5, "2-4c"), (5, 10, "5-9c"),
                           (10, 20, "10-19c"), (20, 100, "20c+")]:
        count = sum(1 for s in spreads if lo <= s < hi)
        print(f"    {label:>6}: {count}")

    # Phase 2: Deep check (orderbook + filters)
    checked = deep_check(client, candidates, max_check=args.max_check)

    # Phase 2b: Filter out distant games (>18h away = mirage liquidity)
    checked = filter_by_hours_to_game(checked, max_hours=18)

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
