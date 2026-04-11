# Logic, Speed & Adverse Selection Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five inherited Kalshi bugs and add two fair-value improvements to raise the live Polymarket round-trip fill rate from ~26% toward 35%+, with a kill-condition tracker to sunset the strategy if gains don't materialize.

**Architecture:** Approach A (fee model fix, OBI near-touch, midpoint history, priority fill quoting) lives in `src/mm/state.py` and `scripts/poly_live_mm.py`. Approach B (fair-value anchoring, adaptive gamma) modifies `skewed_quotes()` and its caller in `_manage_live_quotes`. The kill-condition (B3) writes JSON to `data/session_stats.json` at each session end. All changes to shared code (`src/mm/`) follow TDD; changes to `poly_live_mm.py` include integration tests in `tests/test_poly_live_mm.py`.

**Tech Stack:** Python 3.11, pytest, `src/mm/state.py`, `src/mm/engine.py`, `scripts/poly_live_mm.py`, `src/poly_client.py` (`calculate_maker_fee`)

---

## Background: Key Facts

Before reading code, understand the domain:

- **Binary markets**: YES price + NO price = 100c. Buying YES at 48c and NO at 49c = 3c gross profit (100 - 48 - 49).
- **Polymarket maker fee**: makers receive a REBATE (negative cost). `calculate_maker_fee(price, count)` in `src/poly_client.py` returns a **negative** float (e.g., `-0.12c` per contract). Kalshi is the opposite — makers PAY `0.0175 * P * (1-P) * 100`.
- **OBI microprice**: `fair = bid + spread * (no_depth / total_depth)`. When NO side has more contracts, fair shifts toward ask (more demand for NO = market leans toward NO).
- **Continuous skew**: when holding YES inventory (+net_inv), our YES bid drops and NO bid rises to attract a hedge fill. `skew = net_inv * gamma`. `gamma=0.5` means each extra YES contract shifts our quotes by 0.5c.
- **Round-trip fill rate**: `(paired_fills * 2) / total_fills`. A paired fill = one YES fill + one NO fill that fully hedge. Target: > 35%.

---

## File Map

| File | Tasks |
|------|-------|
| `src/mm/state.py` | Task 1 (skewed_quotes rewrite), Task 2 (compute_gamma) |
| `scripts/poly_live_mm.py` | Task 3 (fee accounting), Task 4 (midpoint cap + OBI near-touch), Task 5 (priority quote on fill), Task 6 (kill condition) |
| `tests/test_mm_state.py` | Task 1, Task 2 tests |
| `tests/test_poly_live_mm.py` | Task 3 tests |

---

## Task 1: Fix skewed_quotes() — fair-value anchoring + Polymarket fee floor

**Files:**
- Modify: `src/mm/state.py:61-91`
- Test: `tests/test_mm_state.py` (append new tests)

**Context:**
- `skewed_quotes()` currently anchors YES bid to `best_yes_bid` and NO bid to `best_no_bid`. The `fair` parameter is accepted but **never used** in price computation.
- The profitability floor uses `0.0175` — the Kalshi maker fee rate. Polymarket makers receive rebates, so this floor incorrectly penalizes wide skews.
- Fix: anchor to `fair` (OBI microprice) with half-spread derived from the current book spread. Simplify the floor to check gross >= 1c only.

- [ ] **Step 1: Write failing tests for fair-value anchoring**

Append to `tests/test_mm_state.py`:

```python
# -- Fair-value anchoring tests (Task 1) --
from src.mm.state import skewed_quotes

def test_skewed_quotes_flat_anchors_to_fair():
    """With no skew, quotes are centered on fair value."""
    # fair=52, spread=4 (best_yes_bid=48, best_no_bid=48 → yes_ask=52)
    # half_spread=2, yes_price = 52-2 = 50, no_price = 48-2 = 46
    yes_p, no_p = skewed_quotes(fair=52.0, best_yes_bid=48, best_no_bid=48,
                                 net_inventory=0, gamma=0.5)
    assert yes_p == 50
    assert no_p == 46

def test_skewed_quotes_fair_above_mid_raises_yes_bid():
    """OBI fair above midpoint → YES bid is above BBO bid."""
    # fair=53, spread=4 → half=2 → yes_price=51 (above BBO bid of 48)
    yes_p, no_p = skewed_quotes(fair=53.0, best_yes_bid=48, best_no_bid=48,
                                 net_inventory=0)
    assert yes_p == 51   # above BBO yes_bid of 48
    assert no_p == 45    # below BBO no_bid of 48 (= 100-52)

def test_skewed_quotes_skew_symmetric():
    """Positive inventory skews YES down, NO up by equal amount."""
    yes_p_flat, no_p_flat = skewed_quotes(fair=50.0, best_yes_bid=48,
                                           best_no_bid=48, net_inventory=0)
    yes_p_long, no_p_long = skewed_quotes(fair=50.0, best_yes_bid=48,
                                           best_no_bid=48, net_inventory=2,
                                           gamma=1.0)
    # Skew = 2 * 1.0 = 2c. YES down by 2, NO up by 2.
    assert yes_p_long == yes_p_flat - 2
    assert no_p_long == no_p_flat + 2

def test_skewed_quotes_polymarket_floor_gross_1c():
    """Floor allows quotes as long as gross >= 1c (no Kalshi fee factor)."""
    # fair=50, spread=2, half=1 → gross = 100 - 49 - 49 = 2c
    # Extreme skew: net_inv=100 would push yes far down. Floor should allow
    # as long as gross >= 1. With half=1 and no skew: 100 - (50-1) - (50-1) = 2.
    yes_p, no_p = skewed_quotes(fair=50.0, best_yes_bid=49, best_no_bid=49,
                                 net_inventory=0, gamma=0.5)
    assert 100 - yes_p - no_p >= 1

def test_skewed_quotes_floor_clamps_extreme_skew():
    """Very large inventory skew gets clamped by profitability floor."""
    # fair=50, spread=4, half=2 → yes_price = 50-2-50 = -2 → clamped
    # Floor loop should reduce skew_raw until gross >= 1.
    yes_p, no_p = skewed_quotes(fair=50.0, best_yes_bid=48, best_no_bid=48,
                                 net_inventory=100, gamma=1.0)
    assert 100 - yes_p - no_p >= 1
    assert yes_p >= 1
    assert no_p >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/yutianyang/polymarket-arb
python -m pytest tests/test_mm_state.py::test_skewed_quotes_flat_anchors_to_fair \
    tests/test_mm_state.py::test_skewed_quotes_fair_above_mid_raises_yes_bid \
    tests/test_mm_state.py::test_skewed_quotes_skew_symmetric \
    tests/test_mm_state.py::test_skewed_quotes_polymarket_floor_gross_1c \
    tests/test_mm_state.py::test_skewed_quotes_floor_clamps_extreme_skew -v
```

Expected: 3-4 FAIL (current code doesn't use `fair`, so anchoring tests will fail).

- [ ] **Step 3: Replace skewed_quotes() in src/mm/state.py:61-91**

Replace the entire function:

```python
def skewed_quotes(fair: float, best_yes_bid: int, best_no_bid: int,
                  net_inventory: int, gamma: float = 0.5,
                  quote_offset: int = 0) -> tuple[int, int]:
    """Compute skewed bid prices for YES and NO sides.

    Anchors to OBI fair value (not BBO). Quotes are placed at:
      YES bid = fair - half_spread - quote_offset - skew
      NO bid  = (100-fair) - half_spread - quote_offset + skew

    Where half_spread = market_spread // 2.

    Positive net_inventory = long YES:
      skew > 0 → YES bid lower (less aggressive) + NO bid higher (more aggressive)

    Profitability floor (Polymarket): gross = 100 - yes - no >= 1c.
    Polymarket makers receive rebates, so no fee-based floor needed.
    """
    skew_raw = net_inventory * gamma

    # Derive half-spread from current book (= yes_ask - best_yes_bid) // 2
    market_spread = 100 - best_no_bid - best_yes_bid  # = yes_ask - best_yes_bid
    half_spread = max(1, market_spread // 2)

    yes_price = max(1, math.floor(fair - half_spread - quote_offset - skew_raw))
    no_price = max(1, math.floor((100 - fair) - half_spread - quote_offset + skew_raw))

    # Profitability floor: gross round-trip must be >= 1c
    # (Polymarket makers earn rebates — no positive fee cost to cover)
    while (100 - yes_price - no_price) < 1 and abs(skew_raw) > 0.1:
        skew_raw *= 0.8
        yes_price = max(1, math.floor(fair - half_spread - quote_offset - skew_raw))
        no_price = max(1, math.floor((100 - fair) - half_spread - quote_offset + skew_raw))

    return yes_price, no_price
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mm_state.py::test_skewed_quotes_flat_anchors_to_fair \
    tests/test_mm_state.py::test_skewed_quotes_fair_above_mid_raises_yes_bid \
    tests/test_mm_state.py::test_skewed_quotes_skew_symmetric \
    tests/test_mm_state.py::test_skewed_quotes_polymarket_floor_gross_1c \
    tests/test_mm_state.py::test_skewed_quotes_floor_clamps_extreme_skew -v
```

Expected: all PASS.

- [ ] **Step 5: Run full state test suite to check for regressions**

```bash
python -m pytest tests/test_mm_state.py tests/test_poly_risk_fixes.py -q
```

Expected: all pass. If `test_soft_close_aggressive_maker_price` or similar fails, check that `soft_close_exit_price` in `src/mm/engine.py` is unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/mm/state.py tests/test_mm_state.py
git commit -m "feat(state): fair-value anchoring + Polymarket fee floor in skewed_quotes"
```

---

## Task 2: Adaptive gamma helper

**Files:**
- Modify: `src/mm/state.py` (append after `hedge_urgency_offset`)
- Modify: `scripts/poly_live_mm.py:1463` (use `compute_gamma` instead of hardcoded 0.5)
- Test: `tests/test_mm_state.py` (append)

**Context:**
- `gamma=0.5` is hardcoded in `_manage_live_quotes` at line 1463.
- When holding unhedged inventory for >5 min, we want more aggressive skew to attract hedge fills sooner (supplements `hedge_urgency_offset` which adds an absolute price improvement; adaptive gamma widens the passive skew).
- Formula: `gamma = min(base + fill_age_minutes * ramp, cap)` with `base=0.5, ramp=0.05, cap=2.0`.
- At 0 min unhedged: gamma = 0.5c/contract (baseline)
- At 10 min unhedged: gamma = 1.0c/contract
- At 20 min unhedged: gamma = 1.5c/contract (capped at 2.0)
- `oldest_fill_time = None` (no open inventory) → gamma = 0.5 (baseline, no skew needed)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mm_state.py`:

```python
# -- Adaptive gamma tests (Task 2) --
from src.mm.state import compute_gamma

def test_compute_gamma_no_inventory():
    """No open inventory → baseline gamma."""
    assert compute_gamma(oldest_fill_time=None) == 0.5

def test_compute_gamma_fresh_fill():
    """Fill just happened → baseline gamma."""
    now = datetime.now(timezone.utc)
    fill_time = now - timedelta(minutes=1)
    g = compute_gamma(oldest_fill_time=fill_time, now=now)
    assert abs(g - 0.55) < 0.01  # 0.5 + 1 * 0.05

def test_compute_gamma_10min_unhedged():
    """10 min unhedged → gamma = 1.0."""
    now = datetime.now(timezone.utc)
    fill_time = now - timedelta(minutes=10)
    g = compute_gamma(oldest_fill_time=fill_time, now=now)
    assert abs(g - 1.0) < 0.01

def test_compute_gamma_cap():
    """100 min unhedged → capped at 2.0."""
    now = datetime.now(timezone.utc)
    fill_time = now - timedelta(minutes=100)
    g = compute_gamma(oldest_fill_time=fill_time, now=now)
    assert g == 2.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mm_state.py::test_compute_gamma_no_inventory \
    tests/test_mm_state.py::test_compute_gamma_fresh_fill \
    tests/test_mm_state.py::test_compute_gamma_10min_unhedged \
    tests/test_mm_state.py::test_compute_gamma_cap -v
```

Expected: FAIL with `ImportError: cannot import name 'compute_gamma'`.

- [ ] **Step 3: Add compute_gamma() to src/mm/state.py**

Append after `hedge_urgency_offset()` (after line 145 in the current file):

```python
def compute_gamma(oldest_fill_time: datetime | None,
                  now: datetime | None = None,
                  base: float = 0.5,
                  ramp: float = 0.05,
                  cap: float = 2.0) -> float:
    """Adaptive inventory-skew gamma based on fill age.

    Ramps up from base when holding unhedged inventory:
      0 min: 0.5c/contract (baseline)
      10 min: 1.0c/contract
      20 min: 1.5c/contract
      30+ min: capped at 2.0c/contract

    Supplements hedge_urgency_offset (which adds an absolute price improvement).
    This widens the passive skew so the reducing side naturally attracts fills.
    """
    if oldest_fill_time is None:
        return base
    if now is None:
        now = datetime.now(timezone.utc)
    elapsed_min = (now - oldest_fill_time).total_seconds() / 60
    return min(base + elapsed_min * ramp, cap)
```

- [ ] **Step 4: Wire compute_gamma into poly_live_mm.py**

In `scripts/poly_live_mm.py`, update the import from `src.mm.state` (around line 41-45) to add `compute_gamma`:

```python
from src.mm.state import (
    MarketState, GlobalState, SimOrder,
    obi_microprice, skewed_quotes, dynamic_spread,
    maker_fee_cents, unrealized_pnl_cents, hedge_urgency_offset,
    compute_gamma,
)
```

In `_manage_live_quotes` at line ~1460-1464, replace the hardcoded gamma:

Find this block:
```python
    # Skewed quotes
    yes_quote, no_quote = skewed_quotes(
        fair=midpoint, best_yes_bid=best_yes_bid,
        best_no_bid=best_no_bid,
        net_inventory=net_inventory, gamma=0.5,
        quote_offset=vol_offset)
```

Replace with:
```python
    # Skewed quotes — adaptive gamma scales with fill age
    gamma = compute_gamma(ms.oldest_fill_time, now)
    yes_quote, no_quote = skewed_quotes(
        fair=midpoint, best_yes_bid=best_yes_bid,
        best_no_bid=best_no_bid,
        net_inventory=net_inventory, gamma=gamma,
        quote_offset=vol_offset)
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_mm_state.py::test_compute_gamma_no_inventory \
    tests/test_mm_state.py::test_compute_gamma_fresh_fill \
    tests/test_mm_state.py::test_compute_gamma_10min_unhedged \
    tests/test_mm_state.py::test_compute_gamma_cap -v
```

Expected: all PASS.

- [ ] **Step 6: Verify no regressions**

```bash
python -m pytest tests/test_mm_state.py tests/test_poly_risk_fixes.py tests/test_mm_engine.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/mm/state.py scripts/poly_live_mm.py tests/test_mm_state.py
git commit -m "feat(state): adaptive gamma — ramps from 0.5 to 2.0c/contract with fill age"
```

---

## Task 3: Fix fee accounting in fill handler

**Files:**
- Modify: `scripts/poly_live_mm.py:950-975`
- Test: `tests/test_poly_live_mm.py` (append)

**Context:**

Current broken flow (lines 950-969):
```python
fee = maker_fee_cents(price, filled)      # Kalshi formula: +0.37c at 50c
ms.total_fees += fee
ms.realized_pnl -= fee                   # WRONG: subtracts a cost that Polymarket doesn't charge

rebate = abs(calculate_maker_fee(price, count=filled))  # rebate tracked separately
rebates_earned[slug] = rebates_earned.get(slug, 0) + rebate  # but NOT added to pnl
```

Problems:
1. `ms.realized_pnl -= fee` subtracts ~0.4c per fill that we never actually pay
2. The rebate we DO receive is tracked in `rebates_earned` but never added to `ms.realized_pnl`
3. At session end: `net_pnl = gross_pnl + total_rebates` double-counts the rebate (adds it on top of gross which already excluded it)

Correct flow:
- `calculate_maker_fee(price, count=filled)` returns **negative** float = rebate (e.g., -0.12c)
- Add the rebate (as positive) to `ms.realized_pnl`
- Set `ms.total_fees` to the negative rebate (we earned, not paid)
- Keep `rebates_earned` for reporting but update `net_pnl` calculation accordingly

At session end (line ~1286-1288), change:
```python
total_rebates = sum(rebates_earned.values())
gross_pnl = gs.total_pnl
net_pnl = gross_pnl + total_rebates   # WRONG: double counts
```
to:
```python
total_rebates = sum(rebates_earned.values())
net_pnl = gs.total_pnl  # realized_pnl already includes rebates
```

- [ ] **Step 1: Write failing tests**

Append to `tests/test_poly_live_mm.py`. First check what imports the file already uses and mimic them. The test validates that after a fill, `ms.realized_pnl` increases by the rebate amount:

```python
# -- Fee accounting tests (Task 3) --
from src.poly_client import calculate_maker_fee

def test_fill_handler_adds_rebate_to_pnl():
    """After a maker fill, realized_pnl should increase by the rebate (not decrease)."""
    from src.mm.state import MarketState
    ms = MarketState(ticker="test-slug")
    initial_pnl = ms.realized_pnl   # 0.0
    
    price = 50
    filled = 2
    # Polymarket rebate is negative from calculate_maker_fee → abs = earned rebate
    expected_rebate = abs(calculate_maker_fee(price, count=filled))
    
    # Simulate the new fill handler logic
    rebate_cents = abs(calculate_maker_fee(price, count=filled))
    ms.total_fees -= rebate_cents
    ms.realized_pnl += rebate_cents
    
    assert ms.realized_pnl > initial_pnl
    assert abs(ms.realized_pnl - expected_rebate) < 0.001
    assert ms.total_fees < 0  # negative = earned


def test_fill_handler_no_fee_subtraction():
    """Old code subtracted maker_fee_cents (Kalshi). New code must NOT do this."""
    from src.mm.state import MarketState, maker_fee_cents
    ms = MarketState(ticker="test-slug")
    
    price = 50
    filled = 1
    old_fee = maker_fee_cents(price, filled)  # 0.4375c — should NOT be subtracted
    
    # Simulate new handler
    rebate_cents = abs(calculate_maker_fee(price, count=filled))
    ms.realized_pnl += rebate_cents
    
    # pnl should be positive (rebate earned), not negative (fee charged)
    assert ms.realized_pnl > 0
    # The old code would have set pnl = -0.4375c — verify that's not the case
    assert ms.realized_pnl != -old_fee
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_poly_live_mm.py::test_fill_handler_adds_rebate_to_pnl \
    tests/test_poly_live_mm.py::test_fill_handler_no_fee_subtraction -v
```

Expected: both FAIL (the new handler isn't implemented yet).

- [ ] **Step 3: Update fill handler in poly_live_mm.py**

Find the fill handler block (~lines 950-975). Replace:

```python
                # Record fill in local state
                fee = maker_fee_cents(price, filled)
                ms.total_fees += fee
                ms.realized_pnl -= fee

                inv_changed_slugs.add(slug)
                ms.total_fills += filled

                if side == "yes":
                    ms.yes_queue.extend([price] * filled)
                else:
                    ms.no_queue.extend([price] * filled)

                if ms.oldest_fill_time is None:
                    ms.oldest_fill_time = datetime.now(timezone.utc)

                inv = ms.net_inventory
                rebate = abs(calculate_maker_fee(price, count=filled))
                rebates_earned[slug] = rebates_earned.get(slug, 0) + rebate

                print(f"  >>> FILL [MAKER] {slug} {side}_bid "
                      f"{filled}@{price}c fee={fee:.2f}c inv={inv} "
                      f"pnl={ms.realized_pnl:.1f}c", flush=True)
```

With:

```python
                # Record fill — Polymarket makers earn REBATE (not pay fee)
                rebate_cents = abs(calculate_maker_fee(price, count=filled))
                ms.total_fees -= rebate_cents   # negative = net earned
                ms.realized_pnl += rebate_cents  # rebate adds to realized P&L

                inv_changed_slugs.add(slug)
                ms.total_fills += filled

                if side == "yes":
                    ms.yes_queue.extend([price] * filled)
                else:
                    ms.no_queue.extend([price] * filled)

                if ms.oldest_fill_time is None:
                    ms.oldest_fill_time = datetime.now(timezone.utc)

                inv = ms.net_inventory
                rebates_earned[slug] = rebates_earned.get(slug, 0) + rebate_cents

                print(f"  >>> FILL [MAKER] {slug} {side}_bid "
                      f"{filled}@{price}c rebate=+{rebate_cents:.2f}c inv={inv} "
                      f"pnl={ms.realized_pnl:.1f}c", flush=True)
```

- [ ] **Step 4: Fix session end net_pnl calculation (~line 1286-1288)**

Find this block:
```python
    total_rebates = sum(rebates_earned.values())
    gross_pnl = gs.total_pnl
    net_pnl = gross_pnl + total_rebates
```

Replace with:
```python
    total_rebates = sum(rebates_earned.values())
    net_pnl = gs.total_pnl  # realized_pnl already includes rebates
```

- [ ] **Step 5: Remove maker_fee_cents from import in poly_live_mm.py**

Find the import (around line 41-45):
```python
from src.mm.state import (
    MarketState, GlobalState, SimOrder,
    obi_microprice, skewed_quotes, dynamic_spread,
    maker_fee_cents, unrealized_pnl_cents, hedge_urgency_offset,
    compute_gamma,
)
```

Remove `maker_fee_cents,`:
```python
from src.mm.state import (
    MarketState, GlobalState, SimOrder,
    obi_microprice, skewed_quotes, dynamic_spread,
    unrealized_pnl_cents, hedge_urgency_offset,
    compute_gamma,
)
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_poly_live_mm.py::test_fill_handler_adds_rebate_to_pnl \
    tests/test_poly_live_mm.py::test_fill_handler_no_fee_subtraction -v
```

Expected: both PASS.

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest tests/test_mm_state.py tests/test_poly_live_mm.py tests/test_poly_risk_fixes.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add scripts/poly_live_mm.py tests/test_poly_live_mm.py
git commit -m "fix(live): correct Polymarket maker rebate accounting — add rebate to pnl instead of subtracting Kalshi fee"
```

---

## Task 4: Fix midpoint history cap + OBI near-touch depth

**Files:**
- Modify: `scripts/poly_live_mm.py:1072-1082`

**Context:**

Two bugs in the orderbook parsing block (~lines 1063-1082):

**Bug A — Midpoint history cap too small:** Line 1081-1082 caps history at 7 entries. `dynamic_spread()` looks back 5 minutes. At 10s per tick, 5 minutes = 30 entries. With only 7 entries, `dynamic_spread()` only sees the last ~70 seconds and the `if len(recent) < 3: return min_spread` guard in `dynamic_spread()` means we often return 2c (minimum) instead of a proper vol estimate.

**Bug B — OBI uses full book depth:** Lines 1072-1073 sum all levels:
```python
yes_depth = sum(q for _, q in yes_bids)
no_depth = sum(q for _, q in no_bids)
```
Whale orders 10-20c from the touch dominate, artificially shifting the OBI microprice away from the actual near-touch imbalance. Fix: filter to levels within 3c of the best bid (touch price).

These two bugs are independent but both live in the same 10-line block, so fix together.

- [ ] **Step 1: Fix midpoint history cap from 7 to 30**

Find in `scripts/poly_live_mm.py` (around line 1080):
```python
                ms.midpoint_history.append((now, midpoint))
                if len(ms.midpoint_history) > 7:
                    ms.midpoint_history.pop(0)
```

Replace with:
```python
                ms.midpoint_history.append((now, midpoint))
                if len(ms.midpoint_history) > 30:
                    ms.midpoint_history.pop(0)
```

- [ ] **Step 2: Fix OBI depth to near-touch only**

Find in `scripts/poly_live_mm.py` (around line 1068-1075):
```python
                best_yes_bid = yes_bids[-1][0]
                best_no_bid = no_bids[-1][0]
                yes_ask = 100 - best_no_bid
                spread = yes_ask - best_yes_bid
                yes_depth = sum(q for _, q in yes_bids)
                no_depth = sum(q for _, q in no_bids)
                midpoint = obi_microprice(best_yes_bid, yes_ask,
                                          yes_depth, no_depth)
```

Replace with:
```python
                best_yes_bid = yes_bids[-1][0]
                best_no_bid = no_bids[-1][0]
                yes_ask = 100 - best_no_bid
                spread = yes_ask - best_yes_bid
                # Near-touch depth only (within 3c of best bid):
                # prevents whale orders deep in book from skewing OBI microprice
                yes_depth = sum(q for p, q in yes_bids if p >= best_yes_bid - 3)
                no_depth = sum(q for p, q in no_bids if p >= best_no_bid - 3)
                midpoint = obi_microprice(best_yes_bid, yes_ask,
                                          yes_depth, no_depth)
```

- [ ] **Step 3: Verify bot still starts (no syntax errors)**

```bash
cd /Users/yutianyang/polymarket-arb
python -c "import scripts.poly_live_mm" 2>&1 | head -20
```

Expected: no output (clean import).

- [ ] **Step 4: Run existing tests for state/engine**

```bash
python -m pytest tests/test_mm_state.py tests/test_mm_engine.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/poly_live_mm.py
git commit -m "fix(live): extend midpoint history to 30 entries; OBI near-touch depth only (3c filter)"
```

---

## Task 5: Priority quote management on fill

**Files:**
- Modify: `scripts/poly_live_mm.py` (main loop, after fill detection block ~line 990)

**Context:**

The main loop processes slugs round-robin (one per tick):
```python
for i, slug in enumerate(active_slugs):
    if cycle % len(active_slugs) != i:
        continue  # only process ONE slug per tick
    ...
    _manage_live_quotes(...)
    inv_changed_slugs.discard(slug)
```

If slug A fills on tick 2 (which processes slug B), slug A's reducing-side order isn't placed until tick 3 — up to `N * 10s` delay (30s with 3 markets). This is critical: every second without a reducing-side order is time an adverse move can run against us.

Fix: after the fill detection + inventory update block, for any slug that had a fill AND is not the round-robin slug this tick, immediately fetch its orderbook and call `_manage_live_quotes`. Guard against processing the round-robin slug twice.

The priority block only runs when there are fills — it doesn't add API calls on normal ticks.

- [ ] **Step 1: Identify insertion point**

The fill loop ends around line 1000 with `ms.paired_fills += len(pairs)`. After this block and before the hedge alert block (~line 1005), insert the priority quote block.

- [ ] **Step 2: Add priority quote management block**

After the pairing loop (after the block ending with `ms.oldest_fill_time = None; ms.skew_activated_at = None`), find the hedge timer alert section:

```python
            # Hedge timer alert: notify Discord if unhedged > 15 min
            now = datetime.now(timezone.utc)
```

Insert the following block BEFORE the hedge timer alert:

```python
            # Priority: immediately manage quotes for fill-affected slugs
            # that are NOT the round-robin slug for this tick.
            # Reduces reducing-side quote latency from ~N*10s to ~0s.
            if inv_changed_slugs:
                rr_slug = active_slugs[cycle % len(active_slugs)] if active_slugs else None
                priority_slugs = [s for s in list(inv_changed_slugs)
                                  if s != rr_slug and gs.markets[s].active]
                for p_slug in priority_slugs:
                    p_ms = gs.markets[p_slug]
                    if p_ms.paused_until and datetime.now(timezone.utc) < p_ms.paused_until:
                        continue
                    try:
                        p_book = client.get_orderbook(p_slug, depth=20)
                    except Exception:
                        continue
                    p_fp = p_book.get("orderbook_fp", {})
                    p_yes_raw = p_fp.get("yes_dollars", [])
                    p_no_raw = p_fp.get("no_dollars", [])
                    if not p_yes_raw or not p_no_raw:
                        continue
                    p_yes_bids = [[round(float(pr) * 100), int(float(q))]
                                  for pr, q in p_yes_raw]
                    p_no_bids = [[round(float(pr) * 100), int(float(q))]
                                 for pr, q in p_no_raw]
                    p_best_yes = p_yes_bids[-1][0]
                    p_best_no = p_no_bids[-1][0]
                    p_yes_ask = 100 - p_best_no
                    p_yes_depth = sum(q for pp, q in p_yes_bids if pp >= p_best_yes - 3)
                    p_no_depth = sum(q for pp, q in p_no_bids if pp >= p_best_no - 3)
                    p_mid = obi_microprice(p_best_yes, p_yes_ask, p_yes_depth, p_no_depth)
                    _manage_live_quotes(
                        live_mgr, p_ms, p_best_yes, p_best_no,
                        p_yes_ask, p_mid, p_yes_bids, p_no_bids,
                        curr_orders, args.size, risk["max_inventory"],
                        time_soft_close=False,
                        inventory_changed=True)
                    inv_changed_slugs.discard(p_slug)
                    print(f"  PRIORITY_QUOTE {p_slug} inv={p_ms.net_inventory}",
                          flush=True)
```

- [ ] **Step 3: Verify no syntax errors**

```bash
python -c "import scripts.poly_live_mm" 2>&1 | head -20
```

Expected: clean.

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/test_mm_state.py tests/test_poly_live_mm.py tests/test_mm_engine.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/poly_live_mm.py
git commit -m "feat(live): priority quote management on fill — immediate reducing-side order without waiting for round-robin"
```

---

## Task 6: Kill condition session stats

**Files:**
- Modify: `scripts/poly_live_mm.py` (add two functions; call at session end ~line 1319)

**Context:**

Kill condition: if avg round-trip fill rate < 35% across the last 5 complete sessions, Discord-alert the operator that the strategy may be unviable.

Session stats are written to `data/session_stats.json` (one JSON array entry per session). The file persists across runs and is NOT gitignored (it accumulates strategy performance history).

`round_trip_rate = (paired_fills * 2) / total_fills` — measures how often a single-side fill gets paired with a hedge fill. `total_fills` = raw side-fills (1 YES = 1). `paired_fills` = completed round-trips. So 10 fills with 3 pairs = rate of 0.6 (60%).

- [ ] **Step 1: Write tests for record_session_stats and check_kill_condition**

Append to `tests/test_poly_live_mm.py`:

```python
# -- Kill condition tests (Task 6) --
import json
import os
import tempfile


def _make_stats_file(sessions: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(sessions, f)


def test_record_session_stats_creates_file():
    """record_session_stats writes to the stats file."""
    from scripts.poly_live_mm import record_session_stats
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        path = tf.name
    os.unlink(path)  # delete so we can test file creation
    try:
        record_session_stats("session-1", total_fills=10, paired_fills=4,
                              stats_path=path)
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["session_id"] == "session-1"
        assert data[0]["total_fills"] == 10
        assert data[0]["paired_fills"] == 4
        assert abs(data[0]["round_trip_rate"] - 0.8) < 0.001
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_record_session_stats_appends():
    """Second call appends without overwriting."""
    from scripts.poly_live_mm import record_session_stats
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        path = tf.name
    os.unlink(path)
    try:
        record_session_stats("s1", total_fills=10, paired_fills=4, stats_path=path)
        record_session_stats("s2", total_fills=8, paired_fills=2, stats_path=path)
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 2
        assert data[1]["session_id"] == "s2"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_check_kill_condition_not_triggered():
    """5 sessions above 35% → no kill condition."""
    from scripts.poly_live_mm import check_kill_condition
    sessions = [{"round_trip_rate": 0.40}] * 5
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        json.dump(sessions, tf)
        path = tf.name
    try:
        result = check_kill_condition(stats_path=path)
        assert result is None
    finally:
        os.unlink(path)


def test_check_kill_condition_triggered():
    """5 sessions all below 35% avg → kill condition fires."""
    from scripts.poly_live_mm import check_kill_condition
    sessions = [{"round_trip_rate": 0.20}] * 5
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        json.dump(sessions, tf)
        path = tf.name
    try:
        result = check_kill_condition(stats_path=path)
        assert result is not None
        assert "35%" in result
    finally:
        os.unlink(path)


def test_check_kill_condition_needs_5_sessions():
    """Fewer than 5 sessions → no kill condition check."""
    from scripts.poly_live_mm import check_kill_condition
    sessions = [{"round_trip_rate": 0.10}] * 4  # only 4 sessions
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tf:
        json.dump(sessions, tf)
        path = tf.name
    try:
        result = check_kill_condition(stats_path=path)
        assert result is None
    finally:
        os.unlink(path)


def test_record_session_stats_zero_fills():
    """Zero fills → round_trip_rate = 0.0, no divide-by-zero."""
    from scripts.poly_live_mm import record_session_stats
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        path = tf.name
    os.unlink(path)
    try:
        record_session_stats("s0", total_fills=0, paired_fills=0, stats_path=path)
        with open(path) as f:
            data = json.load(f)
        assert data[0]["round_trip_rate"] == 0.0
    finally:
        if os.path.exists(path):
            os.unlink(path)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_poly_live_mm.py::test_record_session_stats_creates_file \
    tests/test_poly_live_mm.py::test_check_kill_condition_triggered \
    tests/test_poly_live_mm.py::test_check_kill_condition_needs_5_sessions -v
```

Expected: FAIL with `ImportError: cannot import name 'record_session_stats'`.

- [ ] **Step 3: Add record_session_stats() and check_kill_condition() to poly_live_mm.py**

Add after the constants block (after `_settle_accept_alerted` definition, around line 72). Insert two functions:

```python
SESSION_STATS_PATH = "data/session_stats.json"
KILL_CONDITION_THRESHOLD = 0.35
KILL_CONDITION_MIN_SESSIONS = 5


def record_session_stats(session_id: str, total_fills: int, paired_fills: int,
                          stats_path: str = SESSION_STATS_PATH) -> None:
    """Append this session's round-trip stats to session_stats.json.

    Called at session end. Accumulates across runs to track strategy health.
    round_trip_rate = paired_fills * 2 / total_fills
      (paired_fills = complete round-trips, total_fills = single-side fills)
    """
    rate = (paired_fills * 2 / total_fills) if total_fills > 0 else 0.0
    entry = {
        "session_id": session_id,
        "date": datetime.now(timezone.utc).date().isoformat(),
        "total_fills": total_fills,
        "paired_fills": paired_fills,
        "round_trip_rate": round(rate, 4),
    }
    existing = []
    if os.path.exists(stats_path):
        try:
            with open(stats_path) as f:
                existing = json.load(f)
        except Exception:
            pass
    existing.append(entry)
    os.makedirs(os.path.dirname(stats_path) if os.path.dirname(stats_path) else ".", exist_ok=True)
    with open(stats_path, "w") as f:
        json.dump(existing, f, indent=2)


def check_kill_condition(stats_path: str = SESSION_STATS_PATH,
                          min_sessions: int = KILL_CONDITION_MIN_SESSIONS,
                          threshold: float = KILL_CONDITION_THRESHOLD) -> str | None:
    """Check if strategy kill condition is triggered.

    Returns warning string if avg round_trip_rate over last `min_sessions`
    sessions is below `threshold`. Returns None if not triggered or
    fewer than `min_sessions` sessions recorded.
    """
    if not os.path.exists(stats_path):
        return None
    try:
        with open(stats_path) as f:
            sessions = json.load(f)
    except Exception:
        return None
    if len(sessions) < min_sessions:
        return None
    recent = sessions[-min_sessions:]
    rates = [s.get("round_trip_rate", 0.0) for s in recent]
    avg_rate = sum(rates) / len(rates)
    if avg_rate < threshold:
        return (
            f"Kill condition: avg round-trip rate {avg_rate:.0%} over "
            f"last {min_sessions} sessions is below {threshold:.0%} threshold"
        )
    return None
```

- [ ] **Step 4: Call both functions at session end**

Find the session summary block (~line 1319-1332):

```python
    # Auto-generate session summary
    try:
        from scripts.session_summary import generate_summary
```

Insert BEFORE that block:

```python
    # Record session stats + check kill condition
    total_fills_all = sum(ms.total_fills for ms in gs.markets.values())
    paired_fills_all = sum(ms.paired_fills for ms in gs.markets.values())
    record_session_stats(session_id, total_fills=total_fills_all,
                          paired_fills=paired_fills_all)
    kill_msg = check_kill_condition()
    if kill_msg:
        print(f"\n{'!'*70}")
        print(f"  KILL CONDITION: {kill_msg}")
        print(f"{'!'*70}")
        discord_notify(f"**Strategy Kill Condition** | {kill_msg}")
    else:
        last_rate = (paired_fills_all * 2 / total_fills_all
                     if total_fills_all > 0 else 0.0)
        print(f"\n  Session round-trip rate: {last_rate:.0%} "
              f"(kill threshold: {KILL_CONDITION_THRESHOLD:.0%})")
```

- [ ] **Step 5: Run all tests**

```bash
python -m pytest tests/test_poly_live_mm.py -q
```

Expected: all pass.

- [ ] **Step 6: Verify import clean**

```bash
python -c "import scripts.poly_live_mm" 2>&1 | head -5
```

Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add scripts/poly_live_mm.py tests/test_poly_live_mm.py
git commit -m "feat(live): kill condition tracker — records session stats + alerts if round-trip rate < 35% over 5 sessions"
```

---

## Final Verification

After all 6 tasks complete:

- [ ] **Run full MM test suite**

```bash
python -m pytest tests/test_mm_state.py tests/test_mm_engine.py tests/test_mm_risk.py \
    tests/test_poly_live_mm.py tests/test_poly_risk_fixes.py -q
```

Expected: all pass, no warnings.

- [ ] **Dry-run import check**

```bash
python -c "
import scripts.poly_live_mm
from src.mm.state import skewed_quotes, compute_gamma
from scripts.poly_live_mm import record_session_stats, check_kill_condition
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Final commit if any cleanup needed**

```bash
git add -p  # review any unstaged changes
git commit -m "chore: cleanup after Approach A+B improvements"
```

---

## Post-Implementation: Bot Restart Protocol

Per CLAUDE.md mandatory restart protocol — after committing all changes:

```bash
pkill -9 -f poly_live_mm
# Verify killed: ps aux | grep poly_live_mm
python scripts/poly_live_mm.py --slugs SLUG1,SLUG2 --capital 2500 --size 2 --interval 10
# Verify startup log shows new behavior (adaptive gamma log, rebate log)
```
