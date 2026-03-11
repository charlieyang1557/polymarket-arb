# API Diagnostic & Client Fix — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone diagnostic script that validates Polymarket API responses, captures raw JSON as ground truth, and reports on arbitrage potential — then use findings to fix model/scanner mismatches.

**Architecture:** Standalone diagnostic script (`scripts/diagnose_api.py`) with zero project imports — uses only `requests` + stdlib. Hits Gamma API for events, CLOB API for prices/order books, saves everything to `data/diagnostics/<timestamp>/`. After reviewing output, fix `models.py`, `client.py`, and both scanners to align with actual API response shapes.

**Tech Stack:** Python 3.8+, `requests` library, `json`/`statistics`/`time`/`argparse` (stdlib)

**Spec:** `docs/superpowers/specs/2026-03-11-api-diagnostic-and-client-fix-design.md`

---

## Chunk 1: Diagnostic Script

### Task 1: Scaffold and .gitignore

**Files:**
- Create: `scripts/diagnose_api.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add `data/diagnostics/` to .gitignore**

Append to `.gitignore`:
```
data/diagnostics/
```

- [ ] **Step 2: Create `scripts/diagnose_api.py` with CLI, output dir, and rate limiter**

This is the skeleton: argument parsing, output directory creation, rate limiting, and the `main()` entrypoint that orchestrates all steps. No API calls yet.

```python
"""
Polymarket API Diagnostic Script.

Standalone tool (no project imports) that hits live Polymarket APIs,
captures raw JSON responses, and produces an analysis report.

Usage:
    python scripts/diagnose_api.py              # full run
    python scripts/diagnose_api.py --limit 5    # cap at 5 events (quick test)
"""

import argparse
import json
import os
import signal
import statistics
import sys
import time
from datetime import datetime, timezone

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
    "raw_prices": {},
    "raw_orderbooks": {},
    "errors": {"api_errors": [], "unexpected_fields": [], "missing_fields": []},
    "run_meta": {
        "run_started_at": "",
        "run_finished_at": "",
        "elapsed_seconds": 0.0,
        "api_calls": {"gamma_events": 0, "clob_price": 0, "clob_book": 0, "total": 0},
        "rate_limit_info": {
            "sleeps_triggered": 0,
            "total_sleep_seconds": 0.0,
            "response_headers_sample": {},
        },
    },
    "output_dir": "",
}

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
from typing import Dict, List, Optional, Tuple

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
            resp.raise_for_status()
            return resp.json(), headers
        except requests.RequestException as exc:
            if attempt == 2:
                _state["errors"]["api_errors"].append({
                    "endpoint": f"{path}",
                    "params": str(params),
                    "status_code": getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None,
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


# ---------------------------------------------------------------------------
# Steps (implemented in subsequent tasks)
# ---------------------------------------------------------------------------

def step_fetch_events(limit: Optional[int]) -> List[dict]:
    """Task 2: Fetch all active events from Gamma API."""
    raise NotImplementedError


def step_fetch_prices(neg_risk_events: List[dict]) -> dict:
    """Task 3: Fetch CLOB prices for all tokens in neg-risk events."""
    raise NotImplementedError


def step_fetch_orderbooks(neg_risk_events: List[dict], prices: dict, best_event_id: Optional[str]) -> dict:
    """Task 4: Fetch order books for top events by volume + best opportunity event."""
    raise NotImplementedError


def step_analyze(neg_risk_events: List[dict], prices: dict, orderbooks: dict) -> dict:
    """Task 5: Compute summary analysis."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Polymarket API Diagnostic")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max events to process (for quick test runs)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_interrupt)
    signal.signal(signal.SIGTERM, _handle_interrupt)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    _state["output_dir"] = os.path.join("data", "diagnostics", timestamp)
    os.makedirs(_state["output_dir"], exist_ok=True)
    _state["run_meta"]["run_started_at"] = datetime.now(timezone.utc).isoformat()

    print(f"=== Polymarket API Diagnostic ({timestamp}) ===\n")

    try:
        # 1. Fetch events
        all_events = step_fetch_events(args.limit)

        # 2. Filter to neg-risk with 2+ markets
        neg_risk_events = _filter_neg_risk(all_events)

        # 3. Fetch prices for all tokens
        prices = step_fetch_prices(neg_risk_events)

        # 4. Quick pre-scan to find best opportunity event for order book fetch
        best_event_id = _find_best_event_id(neg_risk_events, prices)

        # 5. Fetch order books
        orderbooks = step_fetch_orderbooks(neg_risk_events, prices, best_event_id)

        # 6. Analyze
        summary = step_analyze(neg_risk_events, prices, orderbooks)

        # 7. Save everything
        _state["run_meta"]["run_finished_at"] = datetime.now(timezone.utc).isoformat()
        start = datetime.fromisoformat(_state["run_meta"]["run_started_at"])
        _state["run_meta"]["elapsed_seconds"] = (datetime.now(timezone.utc) - start).total_seconds()

        out = _state["output_dir"]
        for filename, data in [
            ("raw_events.json", _state["raw_events"]),
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
              f"Rate limit sleeps: {meta['rate_limit_info']['sleeps_triggered']}")

    except Exception as exc:
        print(f"\nFATAL: {exc}")
        _save_partial()
        raise


def _filter_neg_risk(all_events: list[dict]) -> list[dict]:
    """Filter events to those with 2+ neg-risk markets."""
    result = []
    for event in all_events:
        neg_risk_markets = [
            m for m in event.get("markets", [])
            if m.get("negRisk") is True and m.get("active") is True
        ]
        if len(neg_risk_markets) >= 2:
            result.append(event)
    return result


def _find_best_event_id(neg_risk_events: List[dict], prices: dict) -> Optional[str]:
    """Quick scan to find event with lowest price sum (best arb candidate)."""
    best_sum = float("inf")
    best_id = None
    for event in neg_risk_events:
        tokens = _get_first_tokens(event)
        if not tokens:
            continue
        total = sum(prices.get(t, {}).get("ask", 0) or 0 for t in tokens)
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
        clob_ids = m.get("clobTokenIds") or []
        if clob_ids:
            tokens.append(clob_ids[0])
    return tokens


def _print_console_summary(summary: dict):
    """Task 5 helper: print summary to console."""
    raise NotImplementedError


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify skeleton runs and creates output directory**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/diagnose_api.py --limit 1`
Expected: Crashes with `NotImplementedError` from `step_fetch_events`, but the output directory `data/diagnostics/<timestamp>/` should exist.

- [ ] **Step 4: Commit scaffold**

```bash
git add .gitignore scripts/diagnose_api.py
git commit -m "feat: scaffold diagnostic script with CLI, rate limiter, and partial-save"
```

---

### Task 2: Fetch Events from Gamma API

**Files:**
- Modify: `scripts/diagnose_api.py`

- [ ] **Step 1: Implement `step_fetch_events`**

Replace the `step_fetch_events` stub with:

```python
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


EXPECTED_EVENT_FIELDS = {"id", "title", "slug", "markets", "active"}
EXPECTED_MARKET_FIELDS = {"id", "question", "negRisk", "active", "clobTokenIds", "volume24hr"}


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
```

- [ ] **Step 2: Run with `--limit 3` to verify event fetching works**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/diagnose_api.py --limit 3`
Expected: Prints event count, then crashes at `step_fetch_prices` with `NotImplementedError`. Check `data/diagnostics/<latest>/` — `raw_events.json` should NOT exist yet (only saved at end), but the directory should exist.

- [ ] **Step 3: Commit**

```bash
git add scripts/diagnose_api.py
git commit -m "feat(diagnostic): implement event fetching with pagination and field validation"
```

---

### Task 3: Fetch CLOB Prices

**Files:**
- Modify: `scripts/diagnose_api.py`

- [ ] **Step 1: Implement `step_fetch_prices`**

Replace the `step_fetch_prices` stub with:

```python
def step_fetch_prices(neg_risk_events: List[dict]) -> dict:
    """Fetch ask and bid prices for every token in neg-risk events."""
    # Collect all first-token IDs (YES tokens) from neg-risk markets
    token_to_event: dict[str, str] = {}  # token_id -> event_id for debugging
    for event in neg_risk_events:
        for m in event.get("markets", []):
            if not m.get("negRisk") or not m.get("active"):
                continue
            clob_ids = m.get("clobTokenIds") or []
            if clob_ids:
                token_to_event[clob_ids[0]] = str(event.get("id", ""))

    token_ids = list(token_to_event.keys())
    total = len(token_ids)
    print(f"Fetching prices for {total} tokens (need {total * 2} API calls)...")
    print(f"  Estimated time: {total * 2 / MAX_REQUESTS_PER_MINUTE:.1f} minutes at {MAX_REQUESTS_PER_MINUTE} req/min\n")

    prices: dict[str, dict] = {}
    for i, token_id in enumerate(token_ids):
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  Fetching prices... {i + 1}/{total} tokens")

        ask_data, _ = _api_get(CLOB_API_BASE, "/price",
                               params={"token_id": token_id, "side": "BUY"},
                               call_type="clob_price")
        bid_data, _ = _api_get(CLOB_API_BASE, "/price",
                               params={"token_id": token_id, "side": "SELL"},
                               call_type="clob_price")

        prices[token_id] = {
            "ask_raw": ask_data,
            "bid_raw": bid_data,
            "ask": _safe_float(ask_data.get("price")) if ask_data else None,
            "bid": _safe_float(bid_data.get("price")) if bid_data else None,
            "event_id": token_to_event.get(token_id, ""),
        }

    _state["raw_prices"] = prices
    valid = sum(1 for p in prices.values() if p["ask"] is not None and p["ask"] > 0)
    print(f"\n  Prices fetched: {valid}/{total} valid\n")
    return prices


def _safe_float(val) -> Optional[float]:
    """Convert to float, handling strings and None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 2: Run with `--limit 3` to verify price fetching**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/diagnose_api.py --limit 3`
Expected: Prints progress on event fetching, then price fetching. Crashes at `step_fetch_orderbooks`. Check that the rate limiter doesn't trigger on this small run.

- [ ] **Step 3: Commit**

```bash
git add scripts/diagnose_api.py
git commit -m "feat(diagnostic): implement CLOB price fetching with progress tracking"
```

---

### Task 4: Fetch Order Books

**Files:**
- Modify: `scripts/diagnose_api.py`

- [ ] **Step 1: Implement `step_fetch_orderbooks`**

Replace the `step_fetch_orderbooks` stub with:

```python
def step_fetch_orderbooks(neg_risk_events: List[dict], prices: dict, best_event_id: Optional[str]) -> dict:
    """Fetch order books for top 5 events by volume, plus the best opportunity event."""
    # Rank events by total volume
    event_volumes = []
    for event in neg_risk_events:
        total_vol = sum(
            float(m.get("volume24hr", 0) or 0)
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
            clob_ids = m.get("clobTokenIds") or []
            if clob_ids:
                tokens.append((clob_ids[0], str(event.get("id", ""))))

    print(f"Fetching order books for {len(tokens)} tokens across {len(selected_events)} events...")
    orderbooks: dict[str, dict] = {}
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
```

- [ ] **Step 2: Run with `--limit 5` to verify order book fetching**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/diagnose_api.py --limit 5`
Expected: Events fetched, prices fetched, order books fetched. Crashes at `step_analyze`.

- [ ] **Step 3: Commit**

```bash
git add scripts/diagnose_api.py
git commit -m "feat(diagnostic): implement order book fetching for top events"
```

---

### Task 5: Analysis and Console Summary

**Files:**
- Modify: `scripts/diagnose_api.py`

- [ ] **Step 1: Implement `step_analyze` and `_print_console_summary`**

Replace both stubs:

```python
def step_analyze(neg_risk_events: List[dict], prices: dict, orderbooks: dict) -> dict:
    """Compute summary analysis from fetched data."""
    all_events = _state["raw_events"]
    total_events = len(all_events)
    total_neg_risk = len(neg_risk_events)
    events_3plus = sum(
        1 for e in neg_risk_events
        if len([m for m in e.get("markets", []) if m.get("negRisk") and m.get("active")]) >= 3
    )

    # Compute price sums per event
    event_sums = []  # list of (event_id, title, price_sum, volume, tokens_valid, tokens_total, markets_detail)
    for event in neg_risk_events:
        tokens = _get_first_tokens(event)
        if not tokens:
            continue
        total_ask = 0.0
        valid_count = 0
        markets_detail = []
        for m in event.get("markets", []):
            if not m.get("negRisk") or not m.get("active"):
                continue
            clob_ids = m.get("clobTokenIds") or []
            if not clob_ids:
                continue
            token_id = clob_ids[0]
            price_info = prices.get(token_id, {})
            ask = price_info.get("ask")
            if ask is not None and ask > 0:
                total_ask += ask
                valid_count += 1
            markets_detail.append({
                "market_id": m.get("id", ""),
                "question": m.get("question", ""),
                "token_id": token_id,
                "ask": ask,
                "bid": price_info.get("bid"),
                "volume_24h": float(m.get("volume24hr", 0) or 0),
            })

        total_volume = sum(d["volume_24h"] for d in markets_detail)
        event_sums.append({
            "event_id": str(event.get("id", "")),
            "title": event.get("title", ""),
            "price_sum": round(total_ask, 6),
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
    print(f"\nPrice sum distribution (YES asks across all outcomes):")
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

    best = summary.get("best_opportunity")
    if best:
        is_real = best["is_real_opportunity"]
        label = "REAL OPPORTUNITY" if is_real else "Best candidate (below threshold)"
        print(f"\n{'*' * 50}")
        print(f"  {label}:")
        print(f"  Event: {best['event_title']}")
        print(f"  Price sum: {best['price_sum']:.4f}")
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
```

- [ ] **Step 2: Run full diagnostic with `--limit 5` to verify end-to-end**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/diagnose_api.py --limit 5`
Expected: Complete run. All 6 JSON files written to `data/diagnostics/<timestamp>/`. Console prints summary with event counts, price distribution, and best opportunity (or nearest miss).

- [ ] **Step 3: Verify output files exist and are valid JSON**

Run: `cd /Users/yutianyang/polymarket-arb && ls data/diagnostics/ && python -c "import json, glob; [json.load(open(f)) for f in glob.glob('data/diagnostics/*/summary.json')]"`
Expected: Directory listing with timestamp folder. JSON parsing succeeds.

- [ ] **Step 4: Commit**

```bash
git add scripts/diagnose_api.py
git commit -m "feat(diagnostic): implement analysis engine with price sums, volume tiers, and depth"
```

---

### Task 6: Full Live Run

- [ ] **Step 1: Run full diagnostic (no `--limit`) against live Polymarket APIs**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/diagnose_api.py`
Expected: Takes ~8-10 minutes. Prints progress, then summary. All files saved. Note the actual runtime, API call count, and whether rate limiter triggered.

- [ ] **Step 2: Review output — check `raw_events.json` for API field discovery**

Read `data/diagnostics/<latest>/raw_events.json` (just the first event). Note:
- Does the event have `negRisk` or `is_neg_risk` at the event level?
- Do markets have `outcomes` array with names, or just `clobTokenIds`?
- Any unexpected fields?

- [ ] **Step 3: Review output — check `raw_prices.json` for CLOB response format**

Read a sample from `data/diagnostics/<latest>/raw_prices.json`. Note:
- Is `price` a string or number?
- Any other fields in the response?

- [ ] **Step 4: Review output — check `raw_orderbooks.json` for book format**

Read a sample from `data/diagnostics/<latest>/raw_orderbooks.json`. Note:
- Are levels dicts `{"price": "0.5", "size": "100"}` or arrays `[0.5, 100]`?
- Are values strings or numbers?

- [ ] **Step 5: Review `summary.json` — document key findings**

Note: total neg-risk events, completeness rate, price sum distribution, any opportunities found. This data drives all fixes in Chunk 2.

- [ ] **Step 6: No commit needed — this is a data-gathering step**

---

## Chunk 2: Model & Scanner Fixes

> **IMPORTANT:** Tasks 7-10 depend on diagnostic output from Task 6. The exact field names and response formats will be confirmed by reviewing the raw JSON. The code below uses the *expected* field names based on Polymarket documentation and `client.py` patterns. **Adjust based on actual diagnostic findings.**

### Task 7: Fix `src/models.py`

**Files:**
- Modify: `src/models.py`

- [ ] **Step 1: Update `Outcome` model fields**

In `src/models.py`, replace the `Outcome` class (lines 18-23):

```python
# Fields confirmed from Gamma API response — see data/diagnostics/<run_timestamp>/
class Outcome(BaseModel):
    token_id: str
    name: str = ""
    best_ask: float = 0.0
    best_bid: float = 0.0
    volume_24h: float = 0.0
```

(Replace `<run_timestamp>` with the actual timestamp from the diagnostic run.)

- [ ] **Step 2: Fix `Event.is_neg_risk`**

Based on diagnostic findings:
- If Gamma API returns `negRisk` at the event level: populate it in `client.py` (done in Task 8)
- If not: remove `is_neg_risk` from the `Event` model since it's never used (scanners check `market.neg_risk` instead)

Most likely fix — remove from `Event` class (line 44):
```python
# Remove this line:
    is_neg_risk: bool = False
```

- [ ] **Step 3: Verify models parse correctly**

Run: `cd /Users/yutianyang/polymarket-arb && python -c "from src.models import Outcome, Event; o = Outcome(token_id='test', best_ask=0.5, best_bid=0.4); print(o)"`
Expected: Prints the Outcome object without errors.

- [ ] **Step 4: Commit**

```bash
git add src/models.py
git commit -m "fix(models): align Outcome fields with CLOB API response format"
```

---

### Task 8: Fix `src/client.py`

**Files:**
- Modify: `src/client.py`

- [ ] **Step 1: Fix `Outcome` construction in `get_all_active_events` (line 92)**

The client already uses `best_ask`/`best_bid` which now matches the updated model. Verify no Pydantic errors by reading the current line 92 and confirming it matches:

```python
outcomes.append(Outcome(token_id=token, name="", best_ask=0.0, best_bid=0.0))
```

If the Gamma API response includes outcome names (check `raw_events.json` — look for an `outcomes` or `outcomePrices` field), update to extract the name.

- [ ] **Step 2: Fix `Outcome` construction in `get_event_markets` (line 121)**

Same fix — verify `best_ask`/`best_bid` field names match updated model.

- [ ] **Step 3: Fix `get_book_depth` level parsing (lines 191-192)**

Based on `raw_orderbooks.json` findings, update the order book level parsing. If levels are dicts with string values (most likely based on CLOB API conventions):

```python
for level in levels:
    price = float(level.get("price", 0))
    size = float(level.get("size", 0))
```

If levels are already dicts with string values, the existing `float()` cast handles it. If the format is different (arrays, nested dicts), adjust accordingly.

- [ ] **Step 4: Fix `Event.is_neg_risk` population (if keeping the field)**

If the Gamma API returns `negRisk` at the event level (check `raw_events.json`), add to the `Event` construction in `get_all_active_events`:

```python
is_neg_risk=raw.get("negRisk", False),
```

If the field was removed from the model in Task 7, no change needed here.

- [ ] **Step 5: Verify client can fetch and parse**

Run: `cd /Users/yutianyang/polymarket-arb && python -c "from src.client import PolymarketClient; c = PolymarketClient(); events = c.get_all_active_events(); print(f'{len(events)} events, first: {events[0].title if events else \"none\"}')" `
Expected: Prints event count and first event title without Pydantic errors.

- [ ] **Step 6: Commit**

```bash
git add src/client.py
git commit -m "fix(client): align Outcome construction with updated model fields"
```

---

### Task 9: Fix `src/scanner/rebalance.py`

**Files:**
- Modify: `src/scanner/rebalance.py`

- [ ] **Step 1: Fix imports (line 13)**

Replace:
```python
from src.models import ArbitrageOpportunity, ArbitrageType, Event, Market, Outcome
```
With:
```python
from src.models import ArbitrageOpportunity, Event, Market, Outcome
```

- [ ] **Step 2: Fix `ArbitrageOpportunity` constructor (lines 106-118)**

Replace:
```python
        return ArbitrageOpportunity(
            opp_type=ArbitrageType.TYPE1_REBALANCE,
            event_id=event.event_id,
            event_title=event.title,
            markets_involved=populated_markets,
            total_cost=total_ask,
            gross_profit=gross_profit,
            total_fees=total_fees,
            net_profit=net_profit,
            net_profit_pct=net_profit_pct,
            min_liquidity_usd=min_liquidity if min_liquidity != float("inf") else 0.0,
            confidence=1.0,
        )
```
With:
```python
        return ArbitrageOpportunity(
            type="type1_rebalance",
            event_ids=[event.event_id],
            markets=populated_markets,
            total_cost=total_ask,
            expected_profit=net_profit,
            expected_profit_pct=net_profit_pct,
            confidence=1.0,
            details={
                "gross_profit": gross_profit,
                "total_fees": total_fees,
                "min_liquidity_usd": min_liquidity if min_liquidity != float("inf") else 0.0,
                "event_title": event.title,
            },
        )
```

- [ ] **Step 3: Verify Pydantic allows Outcome attribute mutation**

The scanner mutates `Outcome` objects at lines 75-76 (`yes_outcomes[0].best_ask = ask`). Verify this works with Pydantic v2:

Run: `cd /Users/yutianyang/polymarket-arb && python -c "from src.models import Outcome; o = Outcome(token_id='test'); o.best_ask = 0.55; print(f'best_ask={o.best_ask}')"`
Expected: Prints `best_ask=0.55`. If it raises `ValidationError` or `TypeError`, add `model_config = ConfigDict(populate_by_name=True)` or use `model_copy(update={...})` instead of direct assignment.

- [ ] **Step 4: Verify scanner imports without error**

Run: `cd /Users/yutianyang/polymarket-arb && python -c "from src.scanner.rebalance import RebalanceScanner; print('OK')"`
Expected: Prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/scanner/rebalance.py
git commit -m "fix(scanner): align RebalanceScanner with ArbitrageOpportunity model"
```

---

### Task 10: Fix `src/scanner/logical.py`

**Files:**
- Modify: `src/scanner/logical.py`

- [ ] **Step 1: Fix imports (line 14)**

Replace:
```python
from src.models import ArbitrageOpportunity, ArbitrageType, Event
```
With:
```python
from src.models import ArbitrageOpportunity, Event
```

- [ ] **Step 2: Fix defensive price access (lines 116-117)**

Replace:
```python
        ask_a = prices[token_a]["ask"]
        ask_b = prices[token_b]["ask"]
```
With:
```python
        ask_a = prices.get(token_a, {}).get("ask", 0)
        ask_b = prices.get(token_b, {}).get("ask", 0)
```

- [ ] **Step 3: Fix `ArbitrageOpportunity` constructor (lines 143-156)**

Replace:
```python
        return ArbitrageOpportunity(
            opp_type=ArbitrageType.TYPE2_LOGICAL,
            event_id=event_a.event_id,
            event_title=f"{event_a.title} / {event_b.title}",
            markets_involved=[market_a, market_b],
            total_cost=ask_a,
            gross_profit=edge,
            total_fees=fees,
            net_profit=net_profit,
            net_profit_pct=net_profit_pct,
            min_liquidity_usd=min_liquidity,
            confidence=0.7,  # rule-based match is less certain
            notes=f"rule={rule_id}",
        )
```
With:
```python
        return ArbitrageOpportunity(
            type="type2_logical",
            event_ids=[event_a.event_id, event_b.event_id],
            markets=[market_a, market_b],
            total_cost=ask_a,
            expected_profit=net_profit,
            expected_profit_pct=net_profit_pct,
            confidence=0.7,  # rule-based match is less certain
            details={
                "gross_profit": edge,
                "total_fees": fees,
                "min_liquidity_usd": min_liquidity,
                "rule_id": rule_id,
                "event_titles": f"{event_a.title} / {event_b.title}",
            },
        )
```

- [ ] **Step 4: Verify scanner imports without error**

Run: `cd /Users/yutianyang/polymarket-arb && python -c "from src.scanner.logical import LogicalScanner; print('OK')"`
Expected: Prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/scanner/logical.py
git commit -m "fix(scanner): align LogicalScanner with ArbitrageOpportunity model"
```

---

### Task 11: Integration Verification

**Files:**
- None (verification only)

- [ ] **Step 1: Run `scan_once.py` end-to-end**

Run: `cd /Users/yutianyang/polymarket-arb && python scripts/scan_once.py`
Expected: Prints event count, runs both scanners, prints opportunity count. No ImportError, no Pydantic ValidationError. Opportunities may be 0 — that's fine, the point is no crashes.

- [ ] **Step 2: Run pytest to verify all imports resolve**

Run: `cd /Users/yutianyang/polymarket-arb && python -m pytest tests/ -x --co 2>&1 | head -20`
Expected: Test collection succeeds (tests are `NotImplementedError` stubs, but import resolution confirms no stale `ArbitrageType` references).

- [ ] **Step 3: If scan_once.py or pytest fails, diagnose from error output**

Common issues:
- `ValidationError` → a field name still mismatches, check the error message for which field
- `AttributeError` → scanner code references an attribute not on the model
- `TypeError` → constructor receives unexpected keyword argument
- `ImportError` → stale `ArbitrageType` reference in a file not yet fixed

Fix any issues found, re-run.

- [ ] **Step 4: Final commit (if any additional fixes were needed)**

```bash
git add src/models.py src/client.py src/scanner/rebalance.py src/scanner/logical.py
git commit -m "fix: all model and scanner mismatches resolved — scan_once.py runs end-to-end"
```
