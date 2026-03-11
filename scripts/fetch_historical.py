"""
Fetch historical resolved market data from Polymarket for Strategy A1 backtest.

Standalone script (no project imports) — same pattern as diagnose_api.py.

Two-phase approach:
  Phase 1: Bulk fetch market metadata from Gamma API (fast, ~hundreds of API calls)
           Apply aggressive filters, report surviving count.
  Phase 2: Fetch CLOB price history for filtered markets (1 call per market).
           Extract pre-resolution prices for backtest.

Usage:
    python scripts/fetch_historical.py                # Phase 1 only (metadata + filter)
    python scripts/fetch_historical.py --phase2       # Phase 1 + Phase 2 (price history)
    python scripts/fetch_historical.py --phase2 --limit 100  # Phase 2 on first 100
    python scripts/fetch_historical.py --resume       # Resume Phase 2 from partial save
"""

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
MAX_REQUESTS_PER_MINUTE = 55  # conservative to avoid 429s
OUTPUT_DIR = os.path.join("data", "historical")
LOOKBACK_DAYS = 30

# ---------------------------------------------------------------------------
# Filters for Phase 1
# ---------------------------------------------------------------------------
# Outcomes that indicate coin-flip / programmatic markets (no human bias)
_COINFLIP_OUTCOMES = [
    ["Up", "Down"],
    ["Over", "Under"],
    ["Long", "Short"],
    ["Odd", "Even"],
]

# Question patterns that indicate coin-flip / no-bias markets
_COINFLIP_RE = re.compile(
    r"(Up or Down|Over/Under|O/U \d|Odd/Even|"
    r"Map Handicap|Spread:|Total Kills Over|"
    r"Total (Rounds|Maps|Games|Goals|Points|Kills) (Over|O/U)|"
    r"\d+\.\d+ in (Game|Map|Set) \d)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_state = {
    "raw_markets": [],
    "filtered_markets": [],
    "price_history": {},
    "filter_stats": {},
    "errors": [],
    "run_meta": {
        "run_started_at": "",
        "run_finished_at": "",
        "phase": "",
        "api_calls": {"gamma_markets": 0, "clob_prices_history": 0, "total": 0},
        "rate_limit_sleeps": 0,
    },
}

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
_request_times: List[float] = []


def _rate_limit():
    now = time.time()
    _request_times[:] = [t for t in _request_times if now - t < 60]
    if len(_request_times) >= MAX_REQUESTS_PER_MINUTE:
        sleep_for = 60 - (now - _request_times[0]) + 0.5
        print(f"  Rate limit: sleeping {sleep_for:.1f}s...")
        _state["run_meta"]["rate_limit_sleeps"] += 1
        time.sleep(sleep_for)
    _request_times.append(time.time())


def _discord_notify(message: str):
    """Send a Discord notification via webhook (if configured)."""
    hook_path = os.path.join(os.path.dirname(__file__), "..", ".claude", "hooks", "notify_discord.sh")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    try:
        subprocess.run(
            ["bash", hook_path],
            env={**os.environ, "CLAUDE_NOTIFICATION": message},
            timeout=10, capture_output=True,
        )
    except Exception:
        pass  # non-critical


def _api_get(base: str, path: str, params: Optional[dict] = None,
             call_type: str = "") -> Optional[dict]:
    """GET with rate limiting, retry, and error tracking."""
    _rate_limit()
    url = f"{base}{path}"
    _state["run_meta"]["api_calls"]["total"] += 1
    if call_type:
        _state["run_meta"]["api_calls"][call_type] = (
            _state["run_meta"]["api_calls"].get(call_type, 0) + 1
        )

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == 2:
                _state["errors"].append({
                    "endpoint": path,
                    "params": str(params)[:200],
                    "error": str(exc),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return None
            time.sleep(1.0 * (2 ** attempt))
    return None


# ---------------------------------------------------------------------------
# Save / Resume
# ---------------------------------------------------------------------------

def _save_state(phase: str = ""):
    """Save current state to disk."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _state["run_meta"]["run_finished_at"] = datetime.now(timezone.utc).isoformat()
    if phase:
        _state["run_meta"]["phase"] = phase

    files = {
        "raw_markets.json": _state["raw_markets"],
        "filtered_markets.json": _state["filtered_markets"],
        "filter_stats.json": _state["filter_stats"],
        "errors.json": _state["errors"],
        "run_meta.json": _state["run_meta"],
    }
    # Only save price history if we have any
    if _state["price_history"]:
        files["price_history.json"] = _state["price_history"]

    for filename, data in files.items():
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

    print(f"\n  Results saved to {OUTPUT_DIR}/")


def _load_resume_state() -> bool:
    """Load previous Phase 1 results for Phase 2 resume."""
    filtered_path = os.path.join(OUTPUT_DIR, "filtered_markets.json")
    history_path = os.path.join(OUTPUT_DIR, "price_history.json")

    if not os.path.exists(filtered_path):
        print("ERROR: No filtered_markets.json found. Run Phase 1 first.")
        return False

    with open(filtered_path) as f:
        _state["filtered_markets"] = json.load(f)

    if os.path.exists(history_path):
        with open(history_path) as f:
            _state["price_history"] = json.load(f)
        print(f"  Resuming: {len(_state['price_history'])} markets already fetched")

    return True


def _handle_interrupt(sig, frame):
    print("\n\nInterrupted! Saving partial results...")
    _save_state("interrupted")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 1: Fetch and filter market metadata
# ---------------------------------------------------------------------------

def _parse_outcome_prices(raw) -> List[float]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (ValueError, TypeError):
            pass
    return []


def _parse_clob_token_ids(raw) -> List[str]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _is_coinflip(market: dict) -> bool:
    """Check if market is a coin-flip type (Up/Down, Over/Under, etc.)."""
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            outcomes = []

    # Check outcome labels
    if outcomes in _COINFLIP_OUTCOMES:
        return True

    # Check question text
    question = market.get("question", "")
    if _COINFLIP_RE.search(question):
        return True

    return False


def _extract_category_keywords(market: dict) -> List[str]:
    """Extract category-relevant keywords from question and slug."""
    question = market.get("question", "").lower()
    slug = market.get("slug", "").lower()
    text = f"{question} {slug}"

    keywords = []
    patterns = {
        "crypto": r"\b(bitcoin|btc|ethereum|eth|solana|sol|crypto|token|coin|defi|nft|airdrop|blockchain)\b",
        "politics": r"\b(trump|biden|president|election|democrat|republican|congress|senate|governor|vote|primary|political|kamala|harris)\b",
        "sports": r"\b(nba|nfl|mlb|nhl|soccer|football|basketball|baseball|hockey|championship|playoffs|finals|league|premier|serie|bundesliga|ligue|copa|champions)\b",
        "entertainment": r"\b(oscar|grammy|emmy|award|movie|film|album|song|celebrity|kardashian|taylor|swift|beyonce|youtube|tiktok|streamer)\b",
        "science": r"\b(climate|weather|temperature|earthquake|hurricane|storm|space|nasa|mars|moon|asteroid)\b",
        "economics": r"\b(fed|interest rate|inflation|gdp|unemployment|recession|stock|s&p|nasdaq|dow|treasury|tariff)\b",
        "esports": r"\b(esport|dota|league of legends|counter-strike|cs2|valorant|overwatch)\b",
        "geopolitics": r"\b(war|ukraine|russia|china|taiwan|nato|sanctions|ceasefire|treaty|military|missile|nuclear)\b",
    }

    for category, pattern in patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            keywords.append(category)

    return keywords if keywords else ["other"]


def phase1_fetch_and_filter():
    """Fetch all closed markets from Gamma API and apply filters."""
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    cutoff_str = cutoff_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Phase 1: Fetching closed markets (last {LOOKBACK_DAYS} days)")
    print(f"  Cutoff: {cutoff_str}\n")

    # Paginate through all closed markets, most recent first
    all_markets = []
    offset = 0
    page_size = 500
    reached_cutoff = False

    while not reached_cutoff:
        data = _api_get(
            GAMMA_API_BASE, "/markets",
            params={
                "closed": "true",
                "limit": page_size,
                "offset": offset,
                "order": "closedTime",
                "ascending": "false",
            },
            call_type="gamma_markets",
        )
        if data is None or len(data) == 0:
            break

        for m in data:
            closed_time = m.get("closedTime", "")
            if closed_time and closed_time < cutoff_str:
                reached_cutoff = True
                break
            all_markets.append(m)

        print(f"  Fetched page at offset={offset}: {len(data)} markets "
              f"(total: {len(all_markets)}, "
              f"oldest: {data[-1].get('closedTime', '?')[:19]})")

        if len(data) < page_size:
            break
        offset += page_size

    _state["raw_markets"] = all_markets
    print(f"\n  Total closed markets in last {LOOKBACK_DAYS} days: {len(all_markets)}")

    # Apply filters
    stats = {
        "total_raw": len(all_markets),
        "filtered_out": {
            "not_binary_yes_no": 0,
            "coinflip_pattern": 0,
            "low_volume": 0,
            "no_clob_tokens": 0,
            "unresolved": 0,
        },
        "survived": 0,
    }

    filtered = []
    for m in all_markets:
        # Parse outcomes
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError):
                outcomes = []

        # Filter 1: Binary Yes/No only
        if outcomes != ["Yes", "No"]:
            stats["filtered_out"]["not_binary_yes_no"] += 1
            continue

        # Filter 2: Not a coin-flip pattern
        if _is_coinflip(m):
            stats["filtered_out"]["coinflip_pattern"] += 1
            continue

        # Filter 3: Volume > $1,000
        volume = float(m.get("volumeNum", 0) or 0)
        if volume < 1000:
            stats["filtered_out"]["low_volume"] += 1
            continue

        # Filter 4: Has CLOB token IDs
        tokens = _parse_clob_token_ids(m.get("clobTokenIds"))
        if not tokens:
            stats["filtered_out"]["no_clob_tokens"] += 1
            continue

        # Filter 5: Actually resolved (outcomePrices has a winner)
        op = _parse_outcome_prices(m.get("outcomePrices"))
        if len(op) < 2 or not any(abs(p - 1.0) < 0.01 for p in op):
            stats["filtered_out"]["unresolved"] += 1
            continue

        # Determine winner
        did_yes_win = abs(op[0] - 1.0) < 0.01

        # Extract useful fields
        filtered.append({
            "id": m.get("id"),
            "question": m.get("question"),
            "slug": m.get("slug"),
            "clob_token_id_yes": tokens[0],
            "clob_token_id_no": tokens[1] if len(tokens) > 1 else None,
            "did_yes_win": did_yes_win,
            "volume": volume,
            "closed_time": m.get("closedTime"),
            "created_at": m.get("createdAt"),
            "category_keywords": _extract_category_keywords(m),
            "neg_risk": m.get("negRisk", False),
            "group_item_title": m.get("groupItemTitle"),
            "last_trade_price": m.get("lastTradePrice"),
        })

    stats["survived"] = len(filtered)
    _state["filtered_markets"] = filtered
    _state["filter_stats"] = stats

    # Print report
    print(f"\n{'=' * 60}")
    print("PHASE 1 FILTER RESULTS")
    print(f"{'=' * 60}")
    print(f"Total markets (last {LOOKBACK_DAYS} days): {stats['total_raw']:,}")
    print(f"\nFiltered out:")
    for reason, count in stats["filtered_out"].items():
        pct = count / stats["total_raw"] * 100 if stats["total_raw"] else 0
        print(f"  {reason:25s}: {count:>6,} ({pct:.1f}%)")
    total_filtered = sum(stats["filtered_out"].values())
    print(f"  {'TOTAL FILTERED':25s}: {total_filtered:>6,}")
    print(f"\nSurvived: {stats['survived']:,} markets")

    # Category breakdown
    cat_counts: Dict[str, int] = {}
    cat_yes_wins: Dict[str, int] = {}
    for m in filtered:
        for cat in m["category_keywords"]:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            if m["did_yes_win"]:
                cat_yes_wins[cat] = cat_yes_wins.get(cat, 0) + 1

    print(f"\nCategory breakdown (markets may appear in multiple):")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        yes_rate = cat_yes_wins.get(cat, 0) / count * 100 if count else 0
        print(f"  {cat:20s}: {count:>5,} markets  (YES win rate: {yes_rate:.1f}%)")

    # YES win rate overall
    total_yes = sum(1 for m in filtered if m["did_yes_win"])
    overall_rate = total_yes / len(filtered) * 100 if filtered else 0
    print(f"\nOverall YES win rate: {total_yes}/{len(filtered)} = {overall_rate:.1f}%")

    # Estimate Phase 2 time
    est_minutes = len(filtered) / MAX_REQUESTS_PER_MINUTE
    print(f"\nPhase 2 estimate: {len(filtered)} API calls ≈ {est_minutes:.0f} minutes")

    _save_state("phase1_complete")
    return filtered


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------

# Categories to exclude from backtest (not opinion markets)
EXCLUDED_CATEGORIES = {"science"}  # weather precision bets, not human bias

SAMPLE_PER_CATEGORY = 500
RANDOM_SEED = 42


def _stratified_sample(filtered: List[dict]) -> List[dict]:
    """Stratified random sample: up to SAMPLE_PER_CATEGORY per category.

    Each market belongs to its PRIMARY category (first keyword).
    Excludes categories in EXCLUDED_CATEGORIES.
    """
    random.seed(RANDOM_SEED)

    # Group by primary category
    by_category: Dict[str, List[dict]] = {}
    excluded_count = 0
    for m in filtered:
        primary_cat = m["category_keywords"][0]
        if primary_cat in EXCLUDED_CATEGORIES:
            excluded_count += 1
            continue
        by_category.setdefault(primary_cat, []).append(m)

    print(f"\nStratified sampling (seed={RANDOM_SEED}, max {SAMPLE_PER_CATEGORY}/category):")
    print(f"  Excluded categories: {EXCLUDED_CATEGORIES} ({excluded_count} markets)")

    sampled = []
    for cat in sorted(by_category.keys()):
        pool = by_category[cat]
        n = min(len(pool), SAMPLE_PER_CATEGORY)
        chosen = random.sample(pool, n)
        sampled.extend(chosen)
        print(f"  {cat:20s}: {n:>4} / {len(pool):>5} sampled")

    random.shuffle(sampled)  # mix categories for even API load
    print(f"  {'TOTAL':20s}: {len(sampled):>4} markets")
    return sampled


# ---------------------------------------------------------------------------
# Phase 2: Fetch price history for sampled markets
# ---------------------------------------------------------------------------

def _parse_closed_timestamp(closed_time: str) -> Optional[float]:
    """Parse closedTime string to unix timestamp."""
    if not closed_time:
        return None
    try:
        ct = closed_time.replace("+00", "+00:00").replace(" ", "T")
        if not ct.endswith("Z") and "+" not in ct[10:]:
            ct += "+00:00"
        return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_created_timestamp(created_at: str) -> Optional[float]:
    """Parse createdAt string to unix timestamp."""
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _extract_prices_from_history(
    history: List[dict], closed_ts: float, created_ts: Optional[float]
) -> dict:
    """Extract meaningful price points from a market's price history.

    Returns prices at:
    - 24h before close (primary entry signal)
    - midpoint of market lifetime (alternative if market < 48h)
    - earliest available point
    """
    if not history:
        return {"price_24h_before": None, "price_midlife": None,
                "price_earliest": None, "price_source": None}

    # Sort by time (should already be sorted, but be safe)
    points = sorted(history, key=lambda x: x.get("t", 0))

    earliest_t = points[0]["t"]
    earliest_p = points[0]["p"]

    # 24h before close
    target_24h = closed_ts - 86400
    price_24h = None
    for pt in points:
        if pt["t"] <= target_24h:
            price_24h = pt["p"]

    # Midpoint of market lifetime
    if created_ts and created_ts < closed_ts:
        midlife_ts = (created_ts + closed_ts) / 2
    else:
        midlife_ts = (earliest_t + closed_ts) / 2

    price_midlife = None
    best_midlife_dist = float("inf")
    for pt in points:
        dist = abs(pt["t"] - midlife_ts)
        if dist < best_midlife_dist:
            best_midlife_dist = dist
            price_midlife = pt["p"]

    # Choose best available price for backtest
    # Prefer 24h before close; fall back to midlife; then earliest
    if price_24h is not None:
        source = "24h_before_close"
        primary = price_24h
    elif price_midlife is not None:
        source = "midlife"
        primary = price_midlife
    else:
        source = "earliest"
        primary = earliest_p

    return {
        "price_24h_before": price_24h,
        "price_midlife": price_midlife,
        "price_earliest": earliest_p,
        "price_source": source,
        "backtest_price": primary,
    }


def phase2_fetch_prices(filtered: List[dict], limit: Optional[int] = None):
    """Fetch CLOB price history for sampled markets."""
    # Apply stratified sampling
    sampled = _stratified_sample(filtered)
    if limit:
        sampled = sampled[:limit]

    already_done = set(_state["price_history"].keys())
    remaining = [m for m in sampled if str(m["id"]) not in already_done]

    total = len(remaining)
    print(f"\nPhase 2: Fetching price history for {total} markets")
    if already_done:
        print(f"  ({len(already_done)} already fetched, skipping)")
    est_minutes = total / MAX_REQUESTS_PER_MINUTE
    print(f"  Estimated time: {est_minutes:.0f} minutes\n")

    save_interval = 100

    for i, m in enumerate(remaining):
        market_id = str(m["id"])
        token_id = m["clob_token_id_yes"]

        if (i + 1) % 50 == 0 or i == 0:
            elapsed = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(_state["run_meta"]["run_started_at"])).total_seconds()
            done = len(_state["price_history"])
            rate = done / elapsed * 60 if elapsed > 0 else 0
            remaining_est = (total - i) / rate if rate > 0 else 0
            print(f"  Progress: {i + 1}/{total} "
                  f"({done} total done, {rate:.0f}/min, ~{remaining_est:.0f}min remaining)")

        data = _api_get(
            CLOB_API_BASE, "/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": "60"},
            call_type="clob_prices_history",
        )

        history = data.get("history", []) if data else []

        closed_ts = _parse_closed_timestamp(m.get("closed_time", ""))
        created_ts = _parse_created_timestamp(m.get("created_at", ""))

        prices = (
            _extract_prices_from_history(history, closed_ts, created_ts)
            if history and closed_ts
            else {"price_24h_before": None, "price_midlife": None,
                  "price_earliest": None, "price_source": None, "backtest_price": None}
        )

        _state["price_history"][market_id] = {
            "question": m["question"],
            "did_yes_win": m["did_yes_win"],
            "volume": m["volume"],
            "closed_time": m.get("closed_time"),
            "category_keywords": m["category_keywords"],
            "history_points": len(history),
            "group_item_title": m.get("group_item_title"),
            **prices,
        }

        if (i + 1) % save_interval == 0:
            _save_state("phase2_in_progress")

    _save_state("phase2_complete")
    _print_phase2_summary()


def _print_phase2_summary():
    """Print calibration analysis from Phase 2 results."""
    ph = _state["price_history"]
    total_with = sum(1 for v in ph.values() if v.get("backtest_price") is not None)
    total_without = sum(1 for v in ph.values() if v.get("backtest_price") is None)

    print(f"\n{'=' * 60}")
    print("PHASE 2 RESULTS")
    print(f"{'=' * 60}")
    print(f"Markets with price data: {total_with}")
    print(f"Markets without price data: {total_without}")
    print(f"Total: {len(ph)}")

    # Price source breakdown
    sources: Dict[str, int] = {}
    for v in ph.values():
        s = v.get("price_source", "none")
        sources[s] = sources.get(s, 0) + 1
    print(f"\nPrice source breakdown:")
    for s, c in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c}")

    if total_with == 0:
        return

    # Calibration by price bucket
    print(f"\n{'=' * 60}")
    print("CALIBRATION: YES win rate vs YES price (backtest_price)")
    print(f"{'=' * 60}")
    buckets = [
        (0.00, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
        (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
        (0.80, 0.90), (0.90, 1.00),
    ]
    print(f"{'Bucket':>12} {'N':>6} {'YES wins':>9} {'Win rate':>9} "
          f"{'Expected':>9} {'Delta':>8} {'NO Edge':>8}")
    print("-" * 70)

    for lo, hi in buckets:
        count = 0
        wins = 0
        for v in ph.values():
            p = v.get("backtest_price")
            if p is None:
                continue
            if lo <= p < hi:
                count += 1
                if v["did_yes_win"]:
                    wins += 1
        if count > 0:
            rate = wins / count
            expected = (lo + hi) / 2  # bucket midpoint = calibrated rate
            delta = rate - expected
            # NO edge: if YES wins less than expected, buying NO is +EV
            # NO cost = 1 - YES_price; NO payout = $1 if YES loses
            no_cost = 1 - expected
            no_win_rate = 1 - rate
            no_ev = no_win_rate * 1.0 - no_cost  # EV per $1 of NO cost
            no_edge_pct = no_ev / no_cost * 100 if no_cost > 0 else 0

            marker = " ***" if delta < -0.05 and count >= 20 else ""
            print(f"  {lo:.2f}-{hi:.2f}  {count:>6} {wins:>9} {rate:>8.1%} "
                  f"{expected:>8.0%} {delta:>+7.1%} {no_edge_pct:>+7.1f}%{marker}")

    # Per-category calibration for interesting buckets (0.50-0.90)
    print(f"\n{'=' * 60}")
    print("PER-CATEGORY CALIBRATION (YES price 0.50-0.90 only)")
    print(f"{'=' * 60}")
    cat_data: Dict[str, List[Tuple[float, bool]]] = {}
    for v in ph.values():
        p = v.get("backtest_price")
        if p is None or p < 0.50 or p >= 0.90:
            continue
        for cat in v["category_keywords"]:
            cat_data.setdefault(cat, []).append((p, v["did_yes_win"]))

    print(f"{'Category':>15} {'N':>6} {'YES wins':>9} {'Win rate':>9} "
          f"{'Avg price':>10} {'Delta':>8}")
    print("-" * 65)
    for cat in sorted(cat_data.keys()):
        entries = cat_data[cat]
        n = len(entries)
        wins = sum(1 for _, w in entries if w)
        avg_price = sum(p for p, _ in entries) / n
        rate = wins / n
        delta = rate - avg_price
        marker = " ***" if delta < -0.05 and n >= 20 else ""
        print(f"  {cat:>13} {n:>6} {wins:>9} {rate:>8.1%} "
              f"{avg_price:>9.1%} {delta:>+7.1%}{marker}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch historical Polymarket data for A1 backtest"
    )
    parser.add_argument("--phase2", action="store_true",
                        help="Also run Phase 2 (fetch price history)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume Phase 2 from partial save")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit Phase 2 to first N markets")
    parser.add_argument("--days", type=int, default=30,
                        help="Lookback period in days (default: 30)")
    args = parser.parse_args()

    global LOOKBACK_DAYS
    LOOKBACK_DAYS = args.days

    signal.signal(signal.SIGINT, _handle_interrupt)
    signal.signal(signal.SIGTERM, _handle_interrupt)

    _state["run_meta"]["run_started_at"] = datetime.now(timezone.utc).isoformat()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== Polymarket Historical Data Fetch ({timestamp}) ===\n")

    try:
        if args.resume:
            if not _load_resume_state():
                sys.exit(1)
            filtered = _state["filtered_markets"]
            phase2_fetch_prices(filtered, args.limit)
        else:
            filtered = phase1_fetch_and_filter()

            if args.phase2 and filtered:
                phase2_fetch_prices(filtered, args.limit)

        # Final stats
        meta = _state["run_meta"]
        elapsed = 0
        if meta["run_started_at"]:
            start = datetime.fromisoformat(meta["run_started_at"])
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        print(f"\nAPI calls: {meta['api_calls']['total']} | "
              f"Elapsed: {elapsed:.0f}s | "
              f"Rate limit sleeps: {meta['rate_limit_sleeps']}")

        # Discord notification
        _discord_notify(
            f"fetch_historical.py complete: "
            f"{len(_state['price_history'])} markets, "
            f"{meta['api_calls']['total']} API calls, "
            f"{elapsed:.0f}s elapsed"
        )

    except Exception as exc:
        print(f"\nFATAL: {exc}")
        _save_state("error")
        _discord_notify(f"fetch_historical.py FAILED: {exc}")
        raise


if __name__ == "__main__":
    main()
