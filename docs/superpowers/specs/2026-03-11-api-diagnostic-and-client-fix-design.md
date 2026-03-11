# API Diagnostic & Client Fix — Design Spec

## Goal

Validate `client.py` against live Polymarket APIs, capture raw responses as ground truth, then fix model/scanner mismatches based on real data. Produce a diagnostic report covering data completeness, price sum distribution, and arbitrage potential.

## Approach: Diagnose-First, Fix-Second

Build a standalone diagnostic script with no model dependencies, run it against live APIs, use the captured JSON to inform model fixes. This avoids guessing at API response shapes.

---

## Component 1: Diagnostic Script

**File:** `scripts/diagnose_api.py`

**Dependencies:** `requests`, `json`, `time`, `statistics` (stdlib only — no project imports)

### Execution Flow

1. Fetch all active events from Gamma API (`GET /events?active=true&closed=false&limit=500`)
2. Save raw response to `raw_events.json`
3. Identify neg-risk events (markets where `negRisk=true`) with 2+ markets
4. For every `clobTokenId` in those events, fetch CLOB price (`GET /price?token_id=X&side=BUY` and `side=SELL`)
5. Save all price responses to `raw_prices.json`
6. For top 5 events by total volume, fetch full order book for each token (`GET /book?token_id=X`)
7. Save order books to `raw_orderbooks.json`
8. Compute analysis (see Summary section below)
9. Save `summary.json`, `errors.json`, `run_meta.json`

### Output Directory

```
data/diagnostics/YYYY-MM-DD_HHMMSS/
  raw_events.json        # Full Gamma API response (list of event dicts)
  raw_prices.json        # {token_id: {"ask": resp, "bid": resp}} — raw CLOB responses
  raw_orderbooks.json    # {token_id: raw_book_response} — sample order books
  summary.json           # Parsed analysis
  errors.json            # API errors and unexpected fields
  run_meta.json          # Timing, call counts, rate limit headers
```

### summary.json Contents

```json
{
  "total_events": 0,
  "total_neg_risk_events": 0,
  "neg_risk_events_3plus_outcomes": 0,
  "data_completeness": {
    "events_all_prices_valid": 0,
    "events_some_prices_missing": 0,
    "completeness_rate_pct": 0.0
  },
  "price_sum_distribution": {
    "min": 0.0, "max": 0.0, "median": 0.0, "p10": 0.0, "p90": 0.0,
    "count_below_1": 0,
    "count_near_miss_0_98_to_1": 0
  },
  "volume_tiers": {
    "high_gt_10k": {"count": 0, "avg_price_sum": 0.0, "min_price_sum": 0.0, "sums": []},
    "medium_1k_to_10k": {"count": 0, "avg_price_sum": 0.0, "min_price_sum": 0.0, "sums": []},
    "low_lt_1k": {"count": 0, "avg_price_sum": 0.0, "min_price_sum": 0.0, "sums": []}
  },
  "best_opportunity": {
    "event_id": "", "event_title": "", "price_sum": 0.0,
    "gross_profit": 0.0, "net_profit_pct": 0.0,
    "markets": [], "is_real_opportunity": false
  },
  "nearest_miss": {
    "event_id": "", "event_title": "", "price_sum": 0.0,
    "markets": []
  }
}
```

### errors.json Contents

```json
{
  "api_errors": [
    {"endpoint": "", "token_id": "", "status_code": 0, "message": "", "timestamp": ""}
  ],
  "unexpected_fields": [
    {"source": "gamma_event", "event_id": "", "fields": []}
  ],
  "missing_fields": [
    {"source": "gamma_market", "market_id": "", "expected": "", "got": null}
  ]
}
```

### run_meta.json Contents

```json
{
  "run_started_at": "",
  "run_finished_at": "",
  "elapsed_seconds": 0.0,
  "api_calls": {
    "gamma_events": 1,
    "clob_price": 0,
    "clob_book": 0,
    "total": 0
  },
  "rate_limit_info": {
    "sleeps_triggered": 0,
    "total_sleep_seconds": 0.0,
    "response_headers_sample": {}
  }
}
```

### Rate Limiting

- Track all API calls with timestamps (same 60 req/min budget as client.py)
- Log every time the rate limiter triggers a sleep
- Capture rate-limit-related response headers (e.g., `X-RateLimit-Remaining`, `Retry-After`) from a sample of responses to discover actual server-side limits

### Console Output

While running, print progress to stdout:
- `Fetching events... 342 active events found`
- `Identified 47 neg-risk events with 2+ markets (183 tokens total)`
- `Fetching prices... 50/183 tokens (rate limit sleep: 1.2s)...`
- `Fetching order books for top 5 events...`
- `Analysis complete. Results saved to data/diagnostics/2026-03-11_143022/`
- Print best opportunity or nearest miss inline

---

## Component 2: Model & Scanner Fixes

**Executed after reviewing diagnostic output.** No guessing — every field change references the captured JSON.

### Known Fixes

| File | Line(s) | Issue | Fix |
|---|---|---|---|
| `src/models.py` | 18-23 | `Outcome` has `yes_price`/`no_price` | Change to `best_ask`/`best_bid` (or whatever CLOB actually returns) — confirmed from `raw_prices.json` |
| `src/client.py` | 92, 121 | Constructs `Outcome` with fields that don't exist on model | Align with updated `Outcome` fields |
| `src/scanner/rebalance.py` | 13 | Imports non-existent `ArbitrageType` | Remove import, use string literal `"type1_rebalance"` |
| `src/scanner/rebalance.py` | 106-118 | Wrong field names in `ArbitrageOpportunity` constructor | Map: `opp_type` -> `type`, `event_id` -> `event_ids` (list), `net_profit` -> `expected_profit`, `net_profit_pct` -> `expected_profit_pct`, `markets_involved` -> `markets`, remove fields not in model (`gross_profit`, `total_fees`, `min_liquidity_usd`, `event_title`) |
| `src/scanner/logical.py` | 14 (expected) | Same `ArbitrageType` import issue | Same fix — remove import, use string literal |

### Diagnostic-Dependent Fixes

These will be determined from the raw JSON:
- Gamma API event fields: does it return `is_neg_risk` on the event level, or only `negRisk` on markets?
- Gamma API market fields: are outcome names in `outcomes` array or do we only get `clobTokenIds`?
- CLOB price response: is it `{"price": "0.55"}` (string) or `{"price": 0.55}` (float)?
- Order book level format: `{"price": "0.5", "size": "100"}` vs `[0.5, 100]`?

### Provenance Comments

After fixes, add to `src/models.py`:
```python
# Fields confirmed from Gamma API response — see data/diagnostics/YYYY-MM-DD_XXXXXX/
```

---

## Component 3: Integration Verification

**After fixes:** run `scripts/scan_once.py` as end-to-end validation.

### Success Criteria

- `scan_once.py` runs without errors (no ImportError, no Pydantic ValidationError)
- Prints event count and opportunity count
- If opportunities exist: prints edge % and confidence
- No unexpected warnings in logs

### Out of Scope

- No new test files — diagnostic script is our validation
- No changes to evaluator, risk manager, traders, dashboard, or `main.py`
- No caching improvements (informed by rate limit data from diagnostic, done later)
- Type 2 scanner: fix only the import error, no logic changes

---

## Deliverable Order

1. Write `scripts/diagnose_api.py` (standalone, stdlib + requests only)
2. Ensure `data/diagnostics/` is gitignored
3. Run diagnostic, review output
4. Fix `src/models.py` fields based on raw JSON
5. Fix `src/client.py` Outcome construction
6. Fix `src/scanner/rebalance.py` imports and field names
7. Fix `src/scanner/logical.py` import
8. Run `scripts/scan_once.py` to confirm end-to-end
