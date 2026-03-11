"""
Polymarket API Diagnostic Script.

Standalone tool (no project imports) that hits live Polymarket APIs,
captures raw JSON responses, and produces an analysis report.

Usage:
    python scripts/diagnose_api.py                  # midpoint scan, full ask/bid on candidates
    python scripts/diagnose_api.py --fast            # midpoint-only quick scan
    python scripts/diagnose_api.py --full            # full ask/bid for all tokens
    python scripts/diagnose_api.py --limit 50        # cap at 50 events
    python scripts/diagnose_api.py --fast --limit 50 # quick scan, 50 events
"""

import argparse
import json
import os
import signal
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
MAX_REQUESTS_PER_MINUTE = 60
TRADE_FEE_PCT = 0.0001  # 0.01%

# ---------------------------------------------------------------------------
# State — collected data, saved on interrupt
# ---------------------------------------------------------------------------
_state = {
    "raw_events": [],
    "raw_midpoints": {},
    "raw_prices": {},
    "raw_orderbooks": {},
    "errors": {"api_errors": [], "unexpected_fields": [], "missing_fields": []},
    "run_meta": {
        "run_started_at": "",
        "run_finished_at": "",
        "elapsed_seconds": 0.0,
        "api_calls": {"gamma_events": 0, "clob_midpoint": 0, "clob_price": 0, "clob_book": 0, "total": 0},
        "rate_limit_info": {
            "sleeps_triggered": 0,
            "total_sleep_seconds": 0.0,
            "response_headers_sample": {},
        },
    },
    "output_dir": "",
}

# Tokens that returned 404 — skip on subsequent calls
_dead_tokens: set = set()

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
_request_times: List[float] = []


def _rate_limit():
    """Enforce 60 req/min. Returns sleep duration if triggered, else 0."""
    now = time.time()
    _request_times[:] = [t for t in _request_times if now - t < 60]
    if len(_request_times) >= MAX_REQUESTS_PER_MINUTE:
        sleep_for = 60 - (now - _request_times[0]) + 0.1
        print(f"  Rate limit reached, sleeping {sleep_for:.1f}s...")
        _state["run_meta"]["rate_limit_info"]["sleeps_triggered"] += 1
        _state["run_meta"]["rate_limit_info"]["total_sleep_seconds"] += sleep_for
        time.sleep(sleep_for)
    _request_times.append(time.time())


def _api_get(base: str, path: str, params: Optional[dict] = None, call_type: str = "") -> Tuple[Optional[dict], dict]:
    """
    Make a GET request with rate limiting and retry.
    Returns (parsed_json, response_headers_dict).
    On failure returns (None, {}).
    """
    _rate_limit()
    url = f"{base}{path}"
    _state["run_meta"]["api_calls"]["total"] += 1
    if call_type:
        _state["run_meta"]["api_calls"][call_type] = _state["run_meta"]["api_calls"].get(call_type, 0) + 1

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            headers = dict(resp.headers)
            # Capture rate limit headers from first few responses
            if not _state["run_meta"]["rate_limit_info"]["response_headers_sample"]:
                rl_headers = {k: v for k, v in headers.items()
                              if any(x in k.lower() for x in ["rate", "limit", "retry", "remaining"])}
                if rl_headers:
                    _state["run_meta"]["rate_limit_info"]["response_headers_sample"] = rl_headers
            # On 404, mark token as dead and don't retry
            if resp.status_code == 404:
                token_id = (params or {}).get("token_id", "")
                if token_id:
                    _dead_tokens.add(token_id)
                return None, headers
            resp.raise_for_status()
            return resp.json(), headers
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == 2:
                _state["errors"]["api_errors"].append({
                    "endpoint": f"{path}",
                    "params": str(params),
                    "status_code": status,
                    "message": str(exc),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return None, {}
            wait = 1.0 * (2 ** attempt)
            print(f"  Request failed ({exc}), retrying in {wait:.0f}s...")
            time.sleep(wait)
    return None, {}


def _save_partial():
    """Save whatever data we have so far. Called on interrupt or failure."""
    out = _state["output_dir"]
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    _state["run_meta"]["run_finished_at"] = datetime.now(timezone.utc).isoformat()
    if _state["run_meta"]["run_started_at"]:
        start = datetime.fromisoformat(_state["run_meta"]["run_started_at"])
        _state["run_meta"]["elapsed_seconds"] = (datetime.now(timezone.utc) - start).total_seconds()

    for filename, key in [
        ("raw_events.json", "raw_events"),
        ("raw_midpoints.json", "raw_midpoints"),
        ("raw_prices.json", "raw_prices"),
        ("raw_orderbooks.json", "raw_orderbooks"),
        ("errors.json", "errors"),
        ("run_meta.json", "run_meta"),
    ]:
        with open(os.path.join(out, filename), "w") as f:
            json.dump(_state[key], f, indent=2, default=str)
    print(f"\n  Partial results saved to {out}/")


def _handle_interrupt(sig, frame):
    print("\n\nInterrupted! Saving partial results...")
    _save_partial()
    sys.exit(1)


def _safe_float(val) -> Optional[float]:
    """Convert to float, handling strings and None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_clob_token_ids(raw) -> List[str]:
    """Parse clobTokenIds which may be a JSON-encoded string or a list."""
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


# ---------------------------------------------------------------------------
# Step 1: Fetch Events
# ---------------------------------------------------------------------------

EXPECTED_EVENT_FIELDS = {"id", "title", "slug", "markets", "active"}
EXPECTED_MARKET_FIELDS = {"id", "question", "negRisk", "active", "clobTokenIds", "volumeNum", "outcomes", "outcomePrices"}


def step_fetch_events(limit: Optional[int]) -> List[dict]:
    """Fetch all active events from Gamma API with pagination."""
    print("Fetching events from Gamma API...")
    all_events = []
    offset = 0
    page_size = 500

    while True:
        params = {"active": "true", "closed": "false", "limit": page_size, "offset": offset}
        data, _ = _api_get(GAMMA_API_BASE, "/events", params=params, call_type="gamma_events")
        if data is None:
            print(f"  ERROR: Failed to fetch events at offset={offset}")
            break
        all_events.extend(data)
        print(f"  Fetched {len(data)} events (total so far: {len(all_events)})")
        if len(data) < page_size:
            break
        if limit and len(all_events) >= limit:
            break
        offset += page_size

    # Validate expected fields on first event
    if all_events:
        _validate_event_fields(all_events[0])

    if limit and len(all_events) > limit:
        all_events = all_events[:limit]

    print(f"  Total: {len(all_events)} active events" + (f" (limited to {limit})" if limit else "") + "\n")

    _state["raw_events"] = all_events
    return all_events


def _validate_event_fields(event: dict):
    """Check first event for expected fields, log unexpected/missing."""
    event_keys = set(event.keys())
    missing = EXPECTED_EVENT_FIELDS - event_keys
    extra = event_keys - EXPECTED_EVENT_FIELDS
    if missing:
        _state["errors"]["missing_fields"].append({
            "source": "gamma_event", "event_id": str(event.get("id", "")),
            "expected": list(missing), "got": None,
        })
    if extra:
        _state["errors"]["unexpected_fields"].append({
            "source": "gamma_event", "event_id": str(event.get("id", "")),
            "fields": list(extra),
        })
    # Check first market too
    markets = event.get("markets", [])
    if markets:
        mkt = markets[0]
        mkt_keys = set(mkt.keys())
        m_missing = EXPECTED_MARKET_FIELDS - mkt_keys
        m_extra = mkt_keys - EXPECTED_MARKET_FIELDS
        if m_missing:
            _state["errors"]["missing_fields"].append({
                "source": "gamma_market", "market_id": str(mkt.get("id", "")),
                "expected": list(m_missing), "got": None,
            })
        if m_extra:
            _state["errors"]["unexpected_fields"].append({
                "source": "gamma_market", "market_id": str(mkt.get("id", "")),
                "fields": list(m_extra),
            })


# ---------------------------------------------------------------------------
# Step 2a: Extract prices from Gamma outcomePrices (zero API calls)
# ---------------------------------------------------------------------------

def _collect_token_event_map(neg_risk_events: List[dict]) -> Dict[str, str]:
    """Build token_id -> event_id map for first (YES) token in each neg-risk market."""
    token_to_event: Dict[str, str] = {}
    for event in neg_risk_events:
        for m in event.get("markets", []):
            if not m.get("negRisk") or not m.get("active"):
                continue
            clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
            if clob_ids:
                token_to_event[clob_ids[0]] = str(event.get("id", ""))
    return token_to_event


def _parse_outcome_prices(raw) -> List[float]:
    """Parse outcomePrices which is a JSON-encoded string like '[\"0.449\", \"0.551\"]'."""
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


def step_extract_gamma_prices(neg_risk_events: List[dict]) -> Dict[str, dict]:
    """Extract YES prices from Gamma outcomePrices field. Zero API calls needed.

    outcomePrices is [YES_price, NO_price] — YES price ≈ CLOB midpoint.
    """
    print("Extracting prices from Gamma outcomePrices (no API calls needed)...")
    gamma_prices: Dict[str, dict] = {}
    valid = 0
    missing = 0

    for event in neg_risk_events:
        for m in event.get("markets", []):
            if not m.get("negRisk") or not m.get("active"):
                continue
            clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
            if not clob_ids:
                continue
            token_id = clob_ids[0]
            outcome_prices = _parse_outcome_prices(m.get("outcomePrices"))
            yes_price = outcome_prices[0] if outcome_prices else None

            gamma_prices[token_id] = {
                "gamma_yes": yes_price,
                "gamma_no": outcome_prices[1] if len(outcome_prices) > 1 else None,
                "event_id": str(event.get("id", "")),
            }
            if yes_price is not None and yes_price > 0:
                valid += 1
            else:
                missing += 1

    _state["raw_midpoints"] = gamma_prices  # reuse midpoints slot for gamma prices
    print(f"  Extracted {valid} valid prices ({missing} missing/zero)\n")
    return gamma_prices


def _find_candidate_events(neg_risk_events: List[dict], gamma_prices: Dict[str, dict],
                           threshold: float = 1.05) -> List[dict]:
    """Return events where sum of Gamma YES prices < threshold (potential arb candidates)."""
    candidates = []
    for event in neg_risk_events:
        tokens = _get_first_tokens(event)
        if not tokens:
            continue
        price_sum = sum(gamma_prices.get(t, {}).get("gamma_yes", 0) or 0 for t in tokens)
        if 0 < price_sum < threshold:
            candidates.append(event)
    return candidates


# ---------------------------------------------------------------------------
# Step 2b: Fetch Full Prices (ask/bid) — only for candidates
# ---------------------------------------------------------------------------

def step_fetch_prices(neg_risk_events: List[dict], only_tokens: Optional[set] = None) -> dict:
    """Fetch ask and bid prices for YES tokens. Optionally filter to specific tokens."""
    token_to_event = _collect_token_event_map(neg_risk_events)

    if only_tokens is not None:
        token_ids = [t for t in token_to_event if t in only_tokens]
    else:
        token_ids = list(token_to_event.keys())

    # Remove dead tokens
    token_ids = [t for t in token_ids if t not in _dead_tokens]
    total = len(token_ids)
    print(f"Fetching full prices for {total} tokens ({total * 2} API calls)...")
    print(f"  Estimated time: {total * 2 / MAX_REQUESTS_PER_MINUTE:.1f} minutes at {MAX_REQUESTS_PER_MINUTE} req/min\n")

    prices: Dict[str, dict] = {}
    for i, token_id in enumerate(token_ids):
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  Fetching prices... {i + 1}/{total} tokens")

        # side=BUY returns best bid, side=SELL returns best ask
        bid_data, _ = _api_get(CLOB_API_BASE, "/price",
                               params={"token_id": token_id, "side": "BUY"},
                               call_type="clob_price")
        if token_id in _dead_tokens:
            continue  # was marked dead by the bid call
        ask_data, _ = _api_get(CLOB_API_BASE, "/price",
                               params={"token_id": token_id, "side": "SELL"},
                               call_type="clob_price")
        if token_id in _dead_tokens:
            continue

        prices[token_id] = {
            "bid_raw": bid_data,
            "ask_raw": ask_data,
            "ask": _safe_float(ask_data.get("price")) if ask_data else None,
            "bid": _safe_float(bid_data.get("price")) if bid_data else None,
            "event_id": token_to_event.get(token_id, ""),
        }

    _state["raw_prices"] = prices
    valid = sum(1 for p in prices.values() if p["ask"] is not None and p["ask"] > 0)
    print(f"\n  Prices fetched: {valid}/{total} valid\n")
    return prices


# ---------------------------------------------------------------------------
# Step 3: Fetch Order Books
# ---------------------------------------------------------------------------

def step_fetch_orderbooks(neg_risk_events: List[dict], prices: dict, best_event_id: Optional[str]) -> dict:
    """Fetch order books for top 5 events by volume, plus the best opportunity event."""
    # Rank events by total volume
    event_volumes = []
    for event in neg_risk_events:
        total_vol = sum(
            float(m.get("volumeNum", 0) or m.get("volume24hr", 0) or 0)
            for m in event.get("markets", [])
            if m.get("negRisk") and m.get("active")
        )
        event_volumes.append((str(event.get("id", "")), total_vol, event))
    event_volumes.sort(key=lambda x: x[1], reverse=True)

    # Select events: top 5 by volume + best opportunity event
    selected_ids = set()
    selected_events = []
    for eid, vol, event in event_volumes[:5]:
        selected_ids.add(eid)
        selected_events.append(event)
    if best_event_id and best_event_id not in selected_ids:
        for eid, vol, event in event_volumes:
            if eid == best_event_id:
                selected_events.append(event)
                selected_ids.add(eid)
                break

    # Collect tokens from selected events
    tokens = []
    for event in selected_events:
        for m in event.get("markets", []):
            if not m.get("negRisk") or not m.get("active"):
                continue
            clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
            if clob_ids:
                tokens.append((clob_ids[0], str(event.get("id", ""))))

    print(f"Fetching order books for {len(tokens)} tokens across {len(selected_events)} events...")
    orderbooks: Dict[str, dict] = {}
    for i, (token_id, event_id) in enumerate(tokens):
        if (i + 1) % 5 == 0 or i == 0:
            print(f"  Fetching book... {i + 1}/{len(tokens)}")

        book_data, _ = _api_get(CLOB_API_BASE, "/book",
                                params={"token_id": token_id},
                                call_type="clob_book")
        orderbooks[token_id] = {
            "raw": book_data,
            "event_id": event_id,
        }

    _state["raw_orderbooks"] = orderbooks
    print(f"  Fetched {len(orderbooks)} order books\n")
    return orderbooks


# ---------------------------------------------------------------------------
# Step 4: Analysis
# ---------------------------------------------------------------------------

def step_analyze(neg_risk_events: List[dict], prices: dict, orderbooks: dict,
                 midpoints: Optional[Dict[str, dict]] = None, mode: str = "default") -> dict:
    """Compute summary analysis from fetched data.

    mode: 'fast' uses midpoints as price proxy, 'default'/'full' uses ask prices.
    """
    all_events = _state["raw_events"]
    total_events = len(all_events)
    total_neg_risk = len(neg_risk_events)
    events_3plus = sum(
        1 for e in neg_risk_events
        if len([m for m in e.get("markets", []) if m.get("negRisk") and m.get("active")]) >= 3
    )

    # Compute price sums per event
    event_sums = []
    for event in neg_risk_events:
        tokens = _get_first_tokens(event)
        if not tokens:
            continue
        total_price = 0.0
        valid_count = 0
        markets_detail = []
        for m in event.get("markets", []):
            if not m.get("negRisk") or not m.get("active"):
                continue
            clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
            if not clob_ids:
                continue
            token_id = clob_ids[0]

            # Use ask price if available, fall back to Gamma YES price
            price_info = prices.get(token_id, {})
            gamma_info = (midpoints or {}).get(token_id, {})
            ask = price_info.get("ask")
            gamma_yes = gamma_info.get("gamma_yes")
            price_val = ask if ask is not None and ask > 0 else gamma_yes

            if price_val is not None and price_val > 0:
                total_price += price_val
                valid_count += 1
            markets_detail.append({
                "market_id": m.get("id", ""),
                "question": m.get("question", ""),
                "token_id": token_id,
                "ask": ask,
                "bid": price_info.get("bid"),
                "gamma_yes": gamma_yes,
                "price_used": round(price_val, 6) if price_val else None,
                "volume_24h": float(m.get("volumeNum", 0) or m.get("volume24hr", 0) or 0),
            })

        total_volume = sum(d["volume_24h"] for d in markets_detail)
        event_sums.append({
            "event_id": str(event.get("id", "")),
            "title": event.get("title", ""),
            "price_sum": round(total_price, 6),
            "price_source": "gamma" if mode == "fast" else ("clob_ask" if any(
                prices.get(d.get("token_id", ""), {}).get("ask") for d in markets_detail
            ) else "gamma"),
            "volume_24h": total_volume,
            "tokens_valid": valid_count,
            "tokens_total": len(tokens),
            "markets": markets_detail,
        })

    # Data completeness
    all_valid = sum(1 for e in event_sums if e["tokens_valid"] == e["tokens_total"] and e["tokens_total"] > 0)
    some_missing = sum(1 for e in event_sums if e["tokens_valid"] < e["tokens_total"])
    completeness_rate = (all_valid / len(event_sums) * 100) if event_sums else 0.0

    # Price sum distribution
    sums = [e["price_sum"] for e in event_sums if e["price_sum"] > 0]
    below_1 = [s for s in sums if s < 1.0]
    below_1_after_fees = []
    for e in event_sums:
        if e["price_sum"] > 0 and e["price_sum"] < 1.0:
            # Fee is per-trade on each leg's cost; sum of fees = TRADE_FEE_PCT * sum of costs = TRADE_FEE_PCT * price_sum
            fees = e["price_sum"] * TRADE_FEE_PCT
            net = 1.0 - e["price_sum"] - fees
            if net > 0:
                below_1_after_fees.append(e)
    near_miss = [s for s in sums if 0.98 <= s < 1.0]

    dist = {}
    if sums:
        sorted_sums = sorted(sums)
        n = len(sorted_sums)
        dist = {
            "min": sorted_sums[0],
            "max": sorted_sums[-1],
            "median": statistics.median(sorted_sums),
            "p10": sorted_sums[int(n * 0.1)] if n >= 10 else sorted_sums[0],
            "p90": sorted_sums[int(n * 0.9)] if n >= 10 else sorted_sums[-1],
            "count_below_1": len(below_1),
            "count_below_1_after_fees": len(below_1_after_fees),
            "count_near_miss_0_98_to_1": len(near_miss),
        }
    else:
        dist = {"min": 0, "max": 0, "median": 0, "p10": 0, "p90": 0,
                "count_below_1": 0, "count_below_1_after_fees": 0,
                "count_near_miss_0_98_to_1": 0}

    # Volume tiers
    tiers = {"high_gt_10k": [], "medium_1k_to_10k": [], "low_lt_1k": []}
    for e in event_sums:
        if e["price_sum"] <= 0:
            continue
        vol = e["volume_24h"]
        if vol > 10000:
            tiers["high_gt_10k"].append(e["price_sum"])
        elif vol >= 1000:
            tiers["medium_1k_to_10k"].append(e["price_sum"])
        else:
            tiers["low_lt_1k"].append(e["price_sum"])

    volume_tiers = {}
    for tier_name, tier_sums in tiers.items():
        volume_tiers[tier_name] = {
            "count": len(tier_sums),
            "avg_price_sum": round(statistics.mean(tier_sums), 6) if tier_sums else 0,
            "min_price_sum": round(min(tier_sums), 6) if tier_sums else 0,
            "sums": [round(s, 6) for s in sorted(tier_sums)],
        }

    # Best opportunity and nearest miss
    best_opp = None
    nearest_miss = None
    for e in sorted(event_sums, key=lambda x: x["price_sum"]):
        if e["price_sum"] <= 0:
            continue
        if e["price_sum"] < 1.0 and best_opp is None:
            fees = e["price_sum"] * TRADE_FEE_PCT
            gross = 1.0 - e["price_sum"]
            net_pct = (gross - fees) / e["price_sum"] * 100
            # Calculate depth if we have order books for this event
            depth_info = _calc_event_depth(e, orderbooks)
            best_opp = {
                "event_id": e["event_id"],
                "event_title": e["title"],
                "price_sum": e["price_sum"],
                "price_source": e.get("price_source", "unknown"),
                "gross_profit": round(gross, 6),
                "net_profit_pct": round(net_pct, 4),
                "markets": e["markets"],
                "is_real_opportunity": net_pct > 1.0,
                "depth": depth_info,
            }
        elif e["price_sum"] >= 1.0 and nearest_miss is None:
            nearest_miss = {
                "event_id": e["event_id"],
                "event_title": e["title"],
                "price_sum": e["price_sum"],
                "markets": e["markets"],
            }
        if best_opp and nearest_miss:
            break

    return {
        "total_events": total_events,
        "total_neg_risk_events": total_neg_risk,
        "neg_risk_events_3plus_outcomes": events_3plus,
        "data_completeness": {
            "events_all_prices_valid": all_valid,
            "events_some_prices_missing": some_missing,
            "completeness_rate_pct": round(completeness_rate, 1),
        },
        "price_sum_distribution": dist,
        "volume_tiers": volume_tiers,
        "best_opportunity": best_opp,
        "nearest_miss": nearest_miss,
        "_event_sums": event_sums,  # internal, used by console printer
    }


def _calc_event_depth(event_data: dict, orderbooks: dict) -> Optional[dict]:
    """Calculate total USD depth available at arb prices for an event."""
    total_depth = 0.0
    per_market = []
    for m in event_data.get("markets", []):
        token_id = m.get("token_id", "")
        book_entry = orderbooks.get(token_id)
        if not book_entry or not book_entry.get("raw"):
            per_market.append({"token_id": token_id, "depth_usd": None})
            continue
        raw_book = book_entry["raw"]
        asks = raw_book.get("asks", [])
        # Walk asks to calculate depth at reasonable prices
        depth_usd = 0.0
        for level in asks[:10]:  # top 10 levels
            price = _safe_float(level.get("price") if isinstance(level, dict) else None)
            size = _safe_float(level.get("size") if isinstance(level, dict) else None)
            if price and size:
                depth_usd += price * size
        total_depth += depth_usd
        per_market.append({"token_id": token_id, "depth_usd": round(depth_usd, 2)})

    if not per_market:
        return None
    return {
        "total_depth_usd": round(total_depth, 2),
        "min_market_depth_usd": min((m["depth_usd"] for m in per_market if m["depth_usd"] is not None), default=0),
        "per_market": per_market,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filter_neg_risk(all_events: List[dict]) -> List[dict]:
    """Filter events to those with 3+ neg-risk markets.

    Skip 2-market events: binary YES/NO markets always sum to $1.00,
    so Type 1 arb is impossible.
    """
    result = []
    skipped_binary = 0
    for event in all_events:
        neg_risk_markets = [
            m for m in event.get("markets", [])
            if m.get("negRisk") is True and m.get("active") is True
        ]
        if len(neg_risk_markets) == 2:
            skipped_binary += 1
        elif len(neg_risk_markets) >= 3:
            result.append(event)
    if skipped_binary:
        print(f"  (Skipped {skipped_binary} binary 2-outcome events)")
    return result


def _find_best_event_id(neg_risk_events: List[dict], prices: dict,
                        gamma_prices: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """Quick scan to find event with lowest price sum (best arb candidate)."""
    best_sum = float("inf")
    best_id = None
    for event in neg_risk_events:
        tokens = _get_first_tokens(event)
        if not tokens:
            continue
        total = 0.0
        for t in tokens:
            ask = prices.get(t, {}).get("ask", 0) or 0
            if not ask and gamma_prices:
                ask = (gamma_prices or {}).get(t, {}).get("gamma_yes", 0) or 0
            total += ask
        if 0 < total < best_sum:
            best_sum = total
            best_id = str(event.get("id", ""))
    return best_id


def _get_first_tokens(event: dict) -> List[str]:
    """Get first clobTokenId from each neg-risk market in an event."""
    tokens = []
    for m in event.get("markets", []):
        if not m.get("negRisk") or not m.get("active"):
            continue
        clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
        if clob_ids:
            tokens.append(clob_ids[0])
    return tokens


def _print_console_summary(summary: dict):
    """Print key findings to console."""
    print("\n" + "=" * 60)
    print("DIAGNOSTIC RESULTS")
    print("=" * 60)
    print(f"Total events: {summary['total_events']}")
    print(f"Neg-risk events: {summary['total_neg_risk_events']}")
    print(f"  With 3+ outcomes: {summary['neg_risk_events_3plus_outcomes']}")
    print(f"  Data completeness: {summary['data_completeness']['completeness_rate_pct']}%")

    dist = summary["price_sum_distribution"]
    dead = summary.get("dead_tokens", 0)
    if dead:
        print(f"  Dead tokens (404): {dead}")
    print(f"\nPrice sum distribution (across all outcomes):")
    print(f"  Min: {dist['min']:.4f}  Max: {dist['max']:.4f}  Median: {dist['median']:.4f}")
    print(f"  p10: {dist['p10']:.4f}  p90: {dist['p90']:.4f}")
    print(f"  Below $1.00: {dist['count_below_1']} ({dist['count_below_1_after_fees']} after fees)")
    print(f"  Near-miss ($0.98-$1.00): {dist['count_near_miss_0_98_to_1']}")

    print(f"\nVolume tiers:")
    for tier_name, tier_data in summary["volume_tiers"].items():
        label = tier_name.replace("_", " ").replace("gt", ">").replace("lt", "<")
        print(f"  {label}: {tier_data['count']} events, "
              f"avg sum={tier_data['avg_price_sum']:.4f}, "
              f"min sum={tier_data['min_price_sum']:.4f}")

    # Tier breakdown
    tier1_count = sum(1 for e in summary.get("_event_sums", [])
                      if e.get("price_source") == "clob_ask")
    tier2_count = sum(1 for e in summary.get("_event_sums", [])
                      if e.get("price_source") == "gamma" and e["price_sum"] < 1.05 and e["price_sum"] > 0)
    if tier1_count or tier2_count:
        print(f"\nTier breakdown:")
        print(f"  Tier 1 (CLOB verified): {tier1_count} events")
        print(f"  Tier 2 (Gamma only):    {tier2_count} near-miss events")

    best = summary.get("best_opportunity")
    if best:
        is_real = best["is_real_opportunity"]
        label = "REAL OPPORTUNITY" if is_real else "Best candidate (below threshold)"
        print(f"\n{'*' * 50}")
        print(f"  {label}:")
        source = best.get('price_source', 'unknown')
        print(f"  Event: {best['event_title']}")
        print(f"  Price sum: {best['price_sum']:.4f} (source: {source})")
        print(f"  Gross profit: {best['gross_profit']:.4f}")
        print(f"  Net profit: {best['net_profit_pct']:.2f}%")
        depth = best.get("depth")
        if depth:
            print(f"  Order book depth: ${depth['total_depth_usd']:.2f} total "
                  f"(min single market: ${depth['min_market_depth_usd']:.2f})")
        else:
            print(f"  Order book depth: not fetched (check raw_orderbooks.json)")
        print(f"{'*' * 50}")
    else:
        print(f"\n  No events with price sum < $1.00 found")

    miss = summary.get("nearest_miss")
    if miss:
        print(f"\n  Nearest miss: {miss['event_title']}")
        print(f"  Price sum: {miss['price_sum']:.4f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Polymarket API Diagnostic")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max events to process (for quick test runs)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--fast", action="store_true",
                            help="Quick scan: midpoints only, no full ask/bid")
    mode_group.add_argument("--full", action="store_true",
                            help="Full scan: fetch ask/bid for ALL tokens (slow)")
    args = parser.parse_args()

    mode = "fast" if args.fast else ("full" if args.full else "default")

    signal.signal(signal.SIGINT, _handle_interrupt)
    signal.signal(signal.SIGTERM, _handle_interrupt)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    _state["output_dir"] = os.path.join("data", "diagnostics", timestamp)
    os.makedirs(_state["output_dir"], exist_ok=True)
    _state["run_meta"]["run_started_at"] = datetime.now(timezone.utc).isoformat()
    _state["run_meta"]["mode"] = mode

    print(f"=== Polymarket API Diagnostic ({timestamp}) [mode={mode}] ===\n")

    try:
        # 1. Fetch events
        all_events = step_fetch_events(args.limit)

        # 2. Filter to neg-risk with 3+ markets (skip binary events)
        neg_risk_events = _filter_neg_risk(all_events)
        total_tokens = sum(len(_get_first_tokens(e)) for e in neg_risk_events)
        print(f"Identified {len(neg_risk_events)} neg-risk events with 3+ markets ({total_tokens} tokens)\n")

        # 3. Extract prices from Gamma outcomePrices (zero API calls!)
        gamma_prices = step_extract_gamma_prices(neg_risk_events)

        # 4. Two-tier price verification
        prices: Dict[str, dict] = {}
        if mode == "fast":
            print("--fast mode: using Gamma prices only, skipping CLOB ask/bid fetch\n")
        elif mode == "full":
            prices = step_fetch_prices(neg_risk_events)
        else:
            # Tier 1: gamma sum < $0.98 → fetch real CLOB ask/bid (realistic arb candidates)
            tier1 = _find_candidate_events(neg_risk_events, gamma_prices, threshold=0.98)
            # Tier 2: gamma sum $0.98-$1.05 → log only, no CLOB calls
            tier2 = _find_candidate_events(neg_risk_events, gamma_prices, threshold=1.05)
            tier2 = [e for e in tier2 if e not in tier1]

            print(f"Tier 1 (gamma < $0.98, CLOB verify): {len(tier1)} events")
            print(f"Tier 2 (gamma $0.98-$1.05, log only): {len(tier2)} events\n")

            if tier1:
                tier1_tokens = set()
                for event in tier1:
                    tier1_tokens.update(_get_first_tokens(event))
                print(f"Fetching CLOB ask/bid for {len(tier1_tokens)} Tier 1 tokens...\n")
                prices = step_fetch_prices(neg_risk_events, only_tokens=tier1_tokens)
            else:
                print("No Tier 1 candidates — no CLOB calls needed\n")

        # 5. Quick pre-scan to find best opportunity event for order book fetch
        best_event_id = _find_best_event_id(neg_risk_events, prices, gamma_prices)

        # 6. Fetch order books (skip in --fast mode)
        orderbooks: Dict[str, dict] = {}
        if mode != "fast" and best_event_id:
            orderbooks = step_fetch_orderbooks(neg_risk_events, prices, best_event_id)
        elif mode == "fast":
            print("--fast mode: skipping order book fetch\n")

        # 7. Analyze
        summary = step_analyze(neg_risk_events, prices, orderbooks,
                               midpoints=gamma_prices, mode=mode)
        summary["dead_tokens"] = len(_dead_tokens)

        # 8. Save everything
        _state["run_meta"]["run_finished_at"] = datetime.now(timezone.utc).isoformat()
        start = datetime.fromisoformat(_state["run_meta"]["run_started_at"])
        _state["run_meta"]["elapsed_seconds"] = (datetime.now(timezone.utc) - start).total_seconds()
        _state["run_meta"]["dead_tokens"] = len(_dead_tokens)

        out = _state["output_dir"]
        for filename, data in [
            ("raw_events.json", _state["raw_events"]),
            ("raw_midpoints.json", _state["raw_midpoints"]),
            ("raw_prices.json", _state["raw_prices"]),
            ("raw_orderbooks.json", _state["raw_orderbooks"]),
            ("summary.json", summary),
            ("errors.json", _state["errors"]),
            ("run_meta.json", _state["run_meta"]),
        ]:
            with open(os.path.join(out, filename), "w") as f:
                json.dump(data, f, indent=2, default=str)

        _print_console_summary(summary)
        meta = _state["run_meta"]
        print(f"\nResults saved to {out}/")
        print(f"Total API calls: {meta['api_calls']['total']} | "
              f"Elapsed: {meta['elapsed_seconds']:.1f}s | "
              f"Rate limit sleeps: {meta['rate_limit_info']['sleeps_triggered']} | "
              f"Dead tokens: {len(_dead_tokens)}")

    except Exception as exc:
        print(f"\nFATAL: {exc}")
        _save_partial()
        raise


if __name__ == "__main__":
    main()
