# Logic, Speed & Adverse Selection Improvements — Design Spec

**Date:** 2026-04-10
**Status:** Approved for implementation (Approaches A+B, skip C)
**Kill condition:** >35% round-trip fill rate after 5 complete sessions → stop further improvements

---

## Problem Statement

Live Polymarket market-making on a $25 bankroll achieves ~26% round-trip fill rate (paired fills / single-side fills). Informed traders pick off one side before the hedge fills. Two root causes:

1. **Bugs / stale parameters inherited from Kalshi codebase** degrade quote quality silently
2. **Quotes anchor to BBO** rather than fair value, creating adverse selection surface when book moves

---

## Approach A — Bug Fixes & Quick Wins

### A1: Fee model in `skewed_quotes()` profitability floor

**File:** `src/mm/state.py:83-89`

**Problem:** Floor uses `0.0175` (Kalshi maker fee rate). Polymarket makers receive a REBATE (negative cost). Any gross spread > 0 is profitable. The floor incorrectly penalizes wide skews.

**Fix:** Replace the Kalshi fee formula with a simple floor: `100 - yes_price - no_price >= 1`. Remove the 0.0175 constant entirely.

The `fair` parameter is already accepted by `skewed_quotes()` — Task A4 will wire it in.

### A2: Fee accounting in fill handler

**File:** `scripts/poly_live_mm.py:952-969`

**Problem:** Fill handler calls `maker_fee_cents(price, filled)` (Kalshi formula, positive cost) and subtracts it from `realized_pnl`. Then separately computes rebate via `calculate_maker_fee()` and adds to `rebates_earned` only — never to `ms.realized_pnl`. This means:
- `realized_pnl` is reduced by a fee that isn't charged
- Rebate is tracked in a separate dict but never added to PnL

**Fix:** Remove `maker_fee_cents()` call. Use `calculate_maker_fee()` (returns negative = rebate) to compute the rebate. Add rebate magnitude to `ms.realized_pnl`. Update `ms.total_fees` to reflect rebate (it should represent net fee impact — negative means we earned).

### A3: Midpoint history capacity

**File:** `scripts/poly_live_mm.py:1081-1082`

**Problem:** `midpoint_history` is capped at 7 entries. `dynamic_spread()` looks back 5 minutes. At a 10s tick interval, 5 minutes needs 30 entries minimum.

**Fix:** Change cap from 7 to 30.

### A4: OBI near-touch depth only

**Files:** `scripts/poly_live_mm.py:1072-1075`

**Problem:** `yes_depth` and `no_depth` sum the entire order book. Whale orders 10-20c from touch dominate the sum and artificially shift the OBI microprice.

**Fix:** Before summing, filter each book side to levels within 3c of the touch price. This matches the scanner filter logic.

```python
# YES: touch = yes_bids[-1][0]; keep bids within 3c of touch
best_yes = yes_bids[-1][0]
yes_depth = sum(q for p, q in yes_bids if p >= best_yes - 3)
# NO: touch = no_bids[-1][0]; keep bids within 3c of touch
best_no = no_bids[-1][0]
no_depth = sum(q for p, q in no_bids if p >= best_no - 3)
```

### A5: Priority quote management on fill

**File:** `scripts/poly_live_mm.py` (main loop)

**Problem:** The main loop processes slugs round-robin (one per tick). If slug A fills during a tick that processes slug B, slug A's reducing-side order isn't placed until the next tick — up to `N * tick_interval` seconds later (30s with 3 markets).

**Fix:** After the fill detection block, immediately call `_manage_live_quotes()` for any slug that had a fill this tick, regardless of round-robin order. This brings latency from ~30s down to ~0s for reducing-side quote placement.

Guard: only do this if the slug is not already the round-robin slug for this tick (to avoid double-processing).

---

## Approach B — Fair-Value Anchoring + Adaptive Gamma

### B1: Fair-value anchoring in `skewed_quotes()`

**File:** `src/mm/state.py:61-91`

**Problem:** `skewed_quotes()` anchors YES bid to `best_yes_bid` and NO bid to `best_no_bid`. The `fair` parameter is accepted but unused. When BBO shifts (adverse selection), quotes follow the BBO and expose us to adversely-priced fills.

**Fix:** Anchor quotes to `fair` (OBI microprice):

```
half_spread = (100 - fair * 2) / 2  # symmetric half-spread implied by fair vs complement
yes_bid = fair - half_spread - skew   (can be below best_yes_bid = maker sitting away)
no_bid  = (100 - fair) - half_spread + skew
```

This means our YES bid is `fair - half_spread - skew`. If BBO crosses our fair (adverse selection), we sit away from BBO rather than following it deeper into adverse territory.

Backward compatibility: the current `best_yes_bid`/`best_no_bid` parameters remain in the signature — they're no longer used in price computation but are kept for L1 validation context.

### B2: Adaptive gamma

**Files:** `src/mm/state.py` (new helper), `scripts/poly_live_mm.py:1463`

**Problem:** `gamma=0.5` is hardcoded. When we're holding unhedged inventory (especially if price is moving against us), we want more aggressive skew to attract hedge fills sooner.

**Fix:** Compute gamma as:

```
gamma = 0.5 + fill_age_minutes * 0.05 (cap at 2.0)
```

Where `fill_age_minutes = elapsed since oldest_fill_time` (or 0 if no open inventory).

This ramps from 0.5c/contract at baseline to 1.5c/contract after 20 minutes, making the hedging side progressively cheaper without duplicating `hedge_urgency_offset` (which is an additive override on top). Cap at 2.0 to avoid quoting too far from fair.

New helper: `compute_gamma(oldest_fill_time, now, base=0.5, ramp=0.05, cap=2.0) -> float` in `src/mm/state.py`.

### B3: Kill condition tracker

**Files:** `scripts/poly_live_mm.py` (session end), `data/session_stats.json`

**Design:** At session end (or when all markets deactivate), write/append to `data/session_stats.json`:

```json
{
  "session_id": "...",
  "date": "2026-04-10",
  "total_fills": 42,
  "paired_fills": 11,
  "round_trip_rate": 0.524
}
```

On session start, read the last 5 sessions from `session_stats.json`. If all 5 have `round_trip_rate > 0.35` → log "Kill condition NOT triggered, strategy viable". If average < 0.35 → Discord alert: "Kill condition: round-trip rate below 35% threshold over 5 sessions — consider sunsetting strategy."

Only emit the kill condition check if there are ≥ 5 sessions recorded.

---

## Non-Goals (Approach C — skipped)

- WebSocket feed (50ms vs 10s fill detection) — complexity too high for current scale
- Mid-game trading
- Cross-market hedging

---

## File Map

| Task | File(s) | Lines |
|------|---------|-------|
| A1: Fee floor fix | `src/mm/state.py` | 83-89 |
| A2: Fee accounting | `scripts/poly_live_mm.py` | 952-969 |
| A3: Midpoint cap | `scripts/poly_live_mm.py` | 1081-1082 |
| A4: OBI near-touch | `scripts/poly_live_mm.py` | 1072-1075 |
| A5: Priority quote | `scripts/poly_live_mm.py` | ~1025-1200 |
| B1: Fair-value anchor | `src/mm/state.py` | 61-91 |
| B2: Adaptive gamma | `src/mm/state.py` (new fn) + `scripts/poly_live_mm.py` | 1463 |
| B3: Kill condition | `scripts/poly_live_mm.py` + `data/session_stats.json` | session end |

## Tests

All changes to `src/mm/state.py` and `src/mm/engine.py` require tests in `tests/`.

Existing test files:
- `tests/test_mm_state.py` — skewed_quotes, obi_microprice
- `tests/test_poly_live_mm.py` — live MM logic

New tests needed:
- `skewed_quotes()` with Polymarket-correct floor (A1)
- `skewed_quotes()` fair-value anchoring (B1)
- `compute_gamma()` adaptive ramp (B2)
- `obi_microprice()` near-touch depth (A4 — tested via caller)
- Kill condition read/write (B3)
