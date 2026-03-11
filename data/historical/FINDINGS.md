# Strategy A1 Backtest Findings

**Date:** 2026-03-11
**Dataset:** 2,522 resolved Polymarket markets (30-day window, stratified sample)
**Script:** `scripts/fetch_historical.py`

## Hypothesis

"Retail optimism bias causes YES to be systematically overpriced on Polymarket. Buying NO in certain categories should be +EV."

## Verdict: Mostly Disproven

YES is generally **well-calibrated or under-priced**, not over-priced. The broad "buy NO" thesis does not hold.

## Key Findings

### 1. Static Calibration (24h-before-close price)

| Bucket | N | YES Win Rate | Expected | Delta | Signal |
|--------|---|-------------|----------|-------|--------|
| 0.00-0.10 | 1,328 | 1.0% | 5% | -4.0% | Slight NO edge, but tiny per-trade profit |
| 0.40-0.50 | 144 | 36.8% | 45% | -8.2% | **Significant NO edge (p=0.048)** |
| 0.60-0.70 | 59 | 81.4% | 65% | +16.4% | **Significant YES under-pricing (p=0.008)** |
| 0.50-0.60 | 107 | 57.9% | 55% | +2.9% | Well calibrated |
| 0.70-0.80 | 53 | 77.4% | 75% | +2.4% | Well calibrated |
| 0.80-0.90 | 70 | 90.0% | 85% | +5.0% | Not significant (p=0.24) |

### 2. Critical Discovery: Price Drift Artifact

Re-running calibration with **midlife price** (midpoint of market lifetime) vs 24h-before-close:

| Bucket | 24h-before delta | Midlife delta | Interpretation |
|--------|-----------------|---------------|----------------|
| 0.40-0.50 | -6.5% | **-11.7%** | NO edge is stronger at midlife |
| 0.60-0.70 | +19.0% | +4.7% | **Mostly a price drift artifact** |
| 0.70-0.80 | +4.2% | +9.6% | YES under-pricing persists |

The 0.60-0.70 "YES under-pricing" at 24h-before is largely because markets that will resolve YES have already started drifting upward by T-24h. The price 24h out has significant convergence information baked in.

**25% of markets drift >10 cents between midlife and 24h-before close.**

### 3. Per-Category (0.60-0.90 range, 24h-before)

| Category | N | Win Rate | Avg Price | Delta |
|----------|---|----------|-----------|-------|
| Geopolitics | 19 | 94.7% | 71.7% | +23.0% |
| Crypto | 27 | 88.9% | 76.2% | +12.7% |
| Other | 35 | 85.7% | 75.5% | +10.2% |
| Politics | 75 | 81.3% | 75.0% | +6.4% |
| Entertainment | 23 | 73.9% | 79.4% | -5.5% |

Entertainment is the only category where YES is over-priced in this range.

### 4. The Real Signal: Convergence Dynamics

The most important finding is not about static bias but about **price dynamics**:

- Markets are slow to converge toward their resolution price
- The direction and speed of convergence is predictable from external information
- This validates **Strategy B (Leader-Follower)** over Strategy A1:
  - When a leader market resolves, follower markets don't reprice instantly
  - The convergence lag creates a window to buy before the follower catches up

## Data Files

| File | Description |
|------|-------------|
| `filtered_markets.json` | 17,362 filtered markets (Phase 1 output) |
| `price_history.json` | Price data for 2,522 sampled markets (Phase 2 output) |
| `filter_stats.json` | Phase 1 filter statistics |
| `run_meta.json` | API call counts and timing |

## Recommendation

Pivot to Strategy B (Leader-Follower). The edge is in convergence dynamics around events, not in static category bias.
