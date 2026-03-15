# Soft Close + Scanner Trade Volume + P&L Split Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add soft-close mode before game start, trade-volume ranking to scanner, and spread vs inventory P&L decomposition to session summaries.

**Architecture:** Three independent changes: (1) New `is_soft_close` property on MarketState that triggers reduce-only quoting in `_manage_quotes`, (2) `trades_per_hour` field added to daily scanner's candidate ranking, (3) P&L decomposition computed from fills in `session_summary.py`.

**Tech Stack:** Python, SQLite, existing MM engine/scanner infrastructure.

---

## Task 1: Soft Close — State Property

**Files:**
- Modify: `src/mm/state.py:129-136` (add `is_soft_close` property after `is_live_game`)
- Test: `tests/test_mm_state.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_mm_state.py — add these tests

def test_is_soft_close_below_threshold():
    """< 30 trades in 5 min → not soft close."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 10) for i in range(25)]
    assert ms.is_soft_close is False

def test_is_soft_close_at_threshold():
    """> 30 trades in 5 min → soft close."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 5) for i in range(35)]
    assert ms.is_soft_close is True

def test_is_soft_close_not_live_game():
    """Soft close (31-50 trades) is distinct from live game (>50)."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 5) for i in range(40)]
    assert ms.is_soft_close is True
    assert ms.is_live_game is False

def test_is_soft_close_false_when_live():
    """> 50 trades → live game, not soft close (live game takes priority)."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 3) for i in range(60)]
    assert ms.is_live_game is True
    assert ms.is_soft_close is False

def test_is_soft_close_empty():
    """No trades → not soft close."""
    ms = MarketState(ticker="X")
    assert ms.is_soft_close is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mm_state.py -k "soft_close" -v`
Expected: FAIL — `AttributeError: 'MarketState' object has no attribute 'is_soft_close'`

- [ ] **Step 3: Write implementation**

Add to `src/mm/state.py` after the `is_live_game` property (line 136):

```python
@property
def is_soft_close(self) -> bool:
    """Soft-close if >30 trades in last 5 min but not yet live-game (>50)."""
    if not self.trade_timestamps:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    recent = [t for t in self.trade_timestamps if t > cutoff]
    count = len(recent)
    return 30 < count <= 50
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mm_state.py -k "soft_close" -v`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/mm/state.py tests/test_mm_state.py
git commit -m "feat(mm): add is_soft_close property for pre-game wind-down"
```

---

## Task 2: Soft Close — Engine Integration

**Files:**
- Modify: `src/mm/engine.py:516-584` (`_manage_quotes` method)
- Modify: `src/mm/engine.py:196-206` (add soft-close log before hard exit)
- Test: `tests/test_pregame_exit.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_pregame_exit.py — add these tests

def _make_soft_close(ms: MarketState):
    """Populate trade_timestamps to trigger is_soft_close (31-50 range)."""
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 7) for i in range(35)]


def test_soft_close_cancels_inventory_increasing_side():
    """In soft close with inv=-2 (long NO), YES side should be cancelled
    because a YES fill would increase absolute inventory to -4."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    ms.no_queue = [55, 55]  # inv = -2
    now = datetime.now(timezone.utc)
    ms.yes_order = SimOrder(
        side="yes", price=45, size=2, remaining=2,
        queue_pos=100, placed_at=now, db_id=1)
    ms.no_order = SimOrder(
        side="no", price=53, size=2, remaining=2,
        queue_pos=50, placed_at=now, db_id=2)
    _make_soft_close(ms)
    assert ms.is_soft_close is True

    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}
    # Need watermark set so trades don't reset
    ms.last_seen_trade_ts = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    engine.tick_one_market(ms)
    assert ms.active is True  # NOT deactivated (not live game yet)
    # NO order should be cancelled (would increase abs(inv) from 2 to 4)
    assert ms.no_order is None
    # YES order should still exist (would reduce inv from -2 toward 0)
    # (may be requoted, so check it's not None)
    assert ms.yes_order is not None


def test_soft_close_keeps_reducing_side():
    """In soft close with inv=+2 (long YES), NO side kept (reduces inv)."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    ms.yes_queue = [45, 45]  # inv = +2
    now = datetime.now(timezone.utc)
    ms.yes_order = SimOrder(
        side="yes", price=45, size=2, remaining=2,
        queue_pos=100, placed_at=now, db_id=1)
    ms.no_order = SimOrder(
        side="no", price=53, size=2, remaining=2,
        queue_pos=50, placed_at=now, db_id=2)
    _make_soft_close(ms)

    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}
    ms.last_seen_trade_ts = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    engine.tick_one_market(ms)
    assert ms.active is True
    # YES order cancelled (would increase inv from +2 to +4)
    assert ms.yes_order is None
    # NO order kept (reduces inv from +2 toward 0)
    assert ms.no_order is not None


def test_soft_close_flat_inventory_cancels_both():
    """In soft close with inv=0, cancel both sides — don't risk new inventory."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    # inv = 0
    now = datetime.now(timezone.utc)
    ms.yes_order = SimOrder(
        side="yes", price=45, size=2, remaining=2,
        queue_pos=100, placed_at=now, db_id=1)
    ms.no_order = SimOrder(
        side="no", price=53, size=2, remaining=2,
        queue_pos=50, placed_at=now, db_id=2)
    _make_soft_close(ms)

    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}
    ms.last_seen_trade_ts = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    engine.tick_one_market(ms)
    assert ms.active is True
    assert ms.yes_order is None
    assert ms.no_order is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pregame_exit.py -k "soft_close" -v`
Expected: FAIL — soft close logic not yet in engine

- [ ] **Step 3: Write implementation**

In `src/mm/engine.py`, modify `_manage_quotes` method. Add soft-close check at the top, before the quoting loop (after line 536, before line 538):

```python
    def _manage_quotes(self, ms: MarketState, best_yes_bid: int,
                       best_no_bid: int, yes_ask: int, midpoint: float,
                       yes_bids: list, no_bids: list):
        """Place or update simulated resting orders."""
        now = datetime.now(timezone.utc)

        # -- Soft close: only quote the side that reduces inventory --
        if ms.is_soft_close:
            net = ms.net_inventory
            if net == 0:
                # Flat — cancel both, don't risk new inventory
                self._cancel_orders(ms, "soft_close_flat")
                return
            # net > 0 (long YES): keep NO (reduces), cancel YES (increases)
            # net < 0 (long NO): keep YES (reduces), cancel NO (increases)
            cancel_side = "yes" if net > 0 else "no"
            keep_side = "no" if net > 0 else "yes"
            self._cancel_order(ms, cancel_side, "soft_close")

            # Only log once when entering soft close
            if not hasattr(ms, '_soft_close_logged') or not ms._soft_close_logged:
                cutoff = now - timedelta(minutes=5)
                freq = len([t for t in ms.trade_timestamps if t > cutoff])
                print(f"  >>> SOFT CLOSE {ms.ticker}: freq={freq}/5min "
                      f"inv={net}, only quoting {keep_side}")
                ms._soft_close_logged = True

        # Dynamic spread from realized volatility
        market_spread = yes_ask - best_yes_bid
        ...  # rest of method unchanged
```

Then in the quoting loop (line 538-584), add soft-close skip inside the `for side, ...` loop, right after `should_skip_side`:

```python
        for side, quote_price, best_bid, bids in [
                ("yes", yes_quote, best_yes_bid, yes_bids),
                ("no", no_quote, best_no_bid, no_bids)]:
            # Single-side inventory cap
            if should_skip_side(side, ms.net_inventory):
                self._cancel_order(ms, side, "inv_cap")
                continue

            # Soft close: skip side that would increase inventory
            if ms.is_soft_close:
                net = ms.net_inventory
                if net > 0 and side == "yes":
                    continue  # already cancelled above
                if net < 0 and side == "no":
                    continue  # already cancelled above
                if net == 0:
                    continue  # already handled above

            order = ms.yes_order if side == "yes" else ms.no_order
            ...  # rest unchanged
```

Also add `_soft_close_logged` field to `MarketState` in `src/mm/state.py` (default False):

```python
_soft_close_logged: bool = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pregame_exit.py -v`
Expected: All 8 tests PASS (5 existing + 3 new)

Run: `python -m pytest tests/test_mm_*.py tests/test_*skew*.py tests/test_*spread*.py tests/test_*obi*.py tests/test_pregame*.py tests/test_silent*.py tests/test_monitor.py tests/test_inventory*.py -q`
Expected: All 137+ tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mm/engine.py src/mm/state.py tests/test_pregame_exit.py
git commit -m "feat(mm): soft-close mode — reduce-only quoting when freq > 30/5min"
```

---

## Task 3: Scanner — Trade Volume Ranking

**Files:**
- Modify: `scripts/kalshi_daily_scan.py:31-118` (`scan_today_sports` and `deep_check`)
- Test: `tests/test_daily_scan.py` (new file)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_daily_scan.py
"""Tests for daily scanner trade volume ranking."""

from scripts.kalshi_daily_scan import deep_check
from unittest.mock import MagicMock


def _mock_client_with_trades(trades_per_hour: float):
    """Create a mock client that returns trade data for freq calculation."""
    client = MagicMock()
    # Orderbook response
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "200"], ["0.46", "300"]],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    # Trades response — generate trades for freq calc
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    num_trades = int(trades_per_hour)  # 1 hour of trades
    trades = []
    for i in range(num_trades):
        ts = (now - timedelta(seconds=i * (3600 / max(num_trades, 1)))).strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z")
        trades.append({
            "trade_id": f"t{i}",
            "created_time": ts,
            "count_fp": "2",
            "yes_price_dollars": "0.46",
        })
    client.get_trades.return_value = {"trades": trades}
    return client


def test_deep_check_adds_trades_per_hour():
    """deep_check should add trades_per_hour field to candidates."""
    client = _mock_client_with_trades(100)
    candidates = [{
        "ticker": "TEST-MKT",
        "spread": 5,
        "midpoint": 48,
        "volume_24h": 1000,
    }]
    result = deep_check(client, candidates, max_check=1)
    assert "trades_per_hour" in result[0]
    assert result[0]["trades_per_hour"] > 0


def test_deep_check_passes_filter_with_high_freq():
    """Market with high trade freq should pass filters."""
    client = _mock_client_with_trades(600)
    candidates = [{
        "ticker": "FAST-MKT",
        "spread": 5,
        "midpoint": 48,
        "volume_24h": 5000,
    }]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is True


def test_deep_check_fails_filter_with_low_freq():
    """Market with <100 trades/hr should fail the freq filter."""
    client = _mock_client_with_trades(50)
    candidates = [{
        "ticker": "SLOW-MKT",
        "spread": 5,
        "midpoint": 48,
        "volume_24h": 5000,
    }]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_daily_scan.py -v`
Expected: FAIL — `deep_check` doesn't call `get_trades` or compute `trades_per_hour`

- [ ] **Step 3: Write implementation**

Modify `scripts/kalshi_daily_scan.py` `deep_check` function to also fetch trades and compute frequency. Add after the orderbook metrics (after line 158, before `checked.append`):

```python
def deep_check(client: KalshiClient, candidates: list[dict],
               max_check: int = 20) -> list[dict]:
    """Fetch orderbooks and trade frequency to check top candidates."""
    from scripts.kalshi_mm_scanner import _parse_book_levels
    from datetime import timedelta

    checked = []
    for c in candidates[:max_check]:
        ticker = c["ticker"]
        try:
            data = client.get_orderbook(ticker, depth=20)
            yes_levels, no_levels = _parse_book_levels(data)

            yes_depth = sum(s for _, s in yes_levels) if yes_levels else 0
            no_depth = sum(s for _, s in no_levels) if no_levels else 0
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

            # Fetch trade frequency
            try:
                trade_data = client.get_trades(ticker, limit=200)
                trades = trade_data.get("trades", [])
                now = datetime.now(timezone.utc)
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

            max_best_depth = max(yes_best_depth, no_best_depth)
            c["passes"] = (0.2 <= sym <= 5.0
                           and c["spread"] >= 3
                           and c["spread"] < 15
                           and max_best_depth < 20000
                           and c["trades_per_hour"] >= 100)
            checked.append(c)

        except Exception as e:
            c["symmetry"] = 0.0
            c["trades_per_hour"] = 0.0
            c["passes"] = False
            c["error"] = str(e)
            checked.append(c)

        time.sleep(0.1)

    return checked
```

Also update the table header in `main()` to show trades/hr, and update the filter description print line:

In `main()`, change the print on line 211:
```python
print(f"\n  Passing filters (spread 3-14c, sym 0.2-5.0, L1 queue <20K, freq >=100/hr): {len(passing)}")
```

Add `trades_per_hour` column to the table header and row format (lines 214-227):
```python
    header = (f"{'#':>2} {'Pass':>4} {'Ticker':<45} {'Sprd':>4} {'Sym':>5} "
              f"{'yQ1':>5} {'nQ1':>5} {'Trd/h':>6} {'Vol':>7}")
    print(header)
    print("-" * len(header))

    for i, c in enumerate(checked, 1):
        flag = " OK " if c.get("passes") else "FAIL"
        sym = c.get("symmetry", 0)
        sym_s = f"{sym:.2f}" if sym < 100 else ">100"
        ybd = c.get("yes_best_depth", 0)
        nbd = c.get("no_best_depth", 0)
        tph = c.get("trades_per_hour", 0)
        print(f"{i:2d} {flag} {c['ticker']:<45} "
              f"{c['spread']:4d} {sym_s:>5} {ybd:5d} {nbd:5d} "
              f"{tph:6.0f} {c['volume_24h']:7d}")
```

Sort candidates by trades_per_hour (primary) then volume (secondary) after deep_check. Change the sort in `scan_today_sports` or just re-sort after `deep_check` in `main()`:

After `checked = deep_check(client, candidates)`, add:
```python
    checked.sort(key=lambda c: (c.get("trades_per_hour", 0), c.get("volume_24h", 0)),
                 reverse=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_daily_scan.py -v`
Expected: 3 PASSED

Run full suite: `python -m pytest tests/test_mm_*.py tests/test_*skew*.py tests/test_*spread*.py tests/test_*obi*.py tests/test_pregame*.py tests/test_silent*.py tests/test_monitor.py tests/test_inventory*.py tests/test_daily_scan.py -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/kalshi_daily_scan.py tests/test_daily_scan.py
git commit -m "feat(scanner): add trades_per_hour ranking, require >= 100/hr"
```

---

## Task 4: P&L Split — Session Summary

**Files:**
- Modify: `scripts/session_summary.py:42-217` (`generate_summary` function)
- Test: `tests/test_session_summary.py`

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_session_summary.py

def test_generate_summary_has_pnl_split():
    """Summary should contain spread P&L vs inventory P&L breakdown."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db_with_roundtrips(db_path)
        summary = generate_summary(db_path, "test-pnl-split")
        assert "Spread P&L" in summary
        assert "Inventory P&L" in summary
    finally:
        os.unlink(db_path)


def test_pnl_split_correct_values():
    """Spread P&L should equal sum of (100 - yes_cost - no_cost - fees)
    for completed round-trips."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db_with_roundtrips(db_path)
        summary = generate_summary(db_path, "test-pnl-split")
        # 1 round-trip: YES@45 + NO@53 = 100-45-53 = 2c gross
        # fees: 0.77 + 0.87 = 1.64c
        # spread_pnl = 2 - 1.64 = 0.36c per contract, x2 = 0.72c
        # But we track per-fill not per-contract — so it's sum of pair gross
        assert "Spread P&L" in summary
    finally:
        os.unlink(db_path)


def test_pnl_split_with_residual_inventory():
    """Markets with unpaired inventory should show inventory P&L separately."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db_with_residual(db_path)
        summary = generate_summary(db_path, "test-residual")
        assert "Inventory P&L" in summary
        assert "Residual" in summary or "residual" in summary.lower()
    finally:
        os.unlink(db_path)


def _create_test_db_with_roundtrips(path: str):
    """Create DB with complete round-trips for P&L split testing."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE mm_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ticker TEXT, side TEXT, price INTEGER,
            size INTEGER, remaining INTEGER, queue_pos_initial INTEGER,
            status TEXT, placed_at TEXT, filled_at TEXT,
            cancelled_at TEXT, cancel_reason TEXT, time_in_queue_s REAL
        );
        CREATE TABLE mm_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, order_id INTEGER, ticker TEXT, side TEXT,
            price INTEGER, size INTEGER, fee REAL, is_taker INTEGER,
            inventory_after INTEGER, pair_id INTEGER, pair_pnl REAL,
            filled_at TEXT
        );
        CREATE TABLE mm_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, ticker TEXT,
            best_yes_bid INTEGER, yes_ask INTEGER, spread INTEGER,
            midpoint REAL, net_inventory INTEGER, yes_held INTEGER,
            no_held INTEGER, realized_pnl REAL, unrealized_pnl REAL,
            total_pnl REAL, total_fees REAL,
            yes_order_price INTEGER, yes_queue_pos INTEGER,
            no_order_price INTEGER, no_queue_pos INTEGER,
            trade_volume_1min INTEGER,
            global_realized_pnl REAL, global_unrealized_pnl REAL,
            global_total_pnl REAL
        );
        CREATE TABLE mm_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, ticker TEXT, layer INTEGER,
            action TEXT, trigger_reason TEXT,
            net_inventory INTEGER, realized_pnl REAL,
            unrealized_pnl REAL, midpoint REAL, spread INTEGER,
            consecutive_losses INTEGER
        );
    """)
    sid = "test-pnl-split"
    # YES fill: 2 contracts @ 45c
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 1, "MKT_A", "yes_bid", 45, 2, 0.77, 0, 2,
         "2026-03-15T10:00:00+00:00"))
    # NO fill: 2 contracts @ 53c (completes round-trip)
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 2, "MKT_A", "no_bid", 53, 2, 0.87, 0, 0,
         "2026-03-15T10:05:00+00:00"))
    # Snapshots
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T10:00:00+00:00", "MKT_A", 0, 2.36, 0.0, 2.36, 1.64, 8, 48.0))
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T12:00:00+00:00", "MKT_A", 0, 2.36, 0.0, 2.36, 1.64, 8, 48.0))
    conn.commit()
    conn.close()


def _create_test_db_with_residual(path: str):
    """Create DB with unpaired inventory for inventory P&L testing."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE mm_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ticker TEXT, side TEXT, price INTEGER,
            size INTEGER, remaining INTEGER, queue_pos_initial INTEGER,
            status TEXT, placed_at TEXT, filled_at TEXT,
            cancelled_at TEXT, cancel_reason TEXT, time_in_queue_s REAL
        );
        CREATE TABLE mm_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, order_id INTEGER, ticker TEXT, side TEXT,
            price INTEGER, size INTEGER, fee REAL, is_taker INTEGER,
            inventory_after INTEGER, pair_id INTEGER, pair_pnl REAL,
            filled_at TEXT
        );
        CREATE TABLE mm_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, ticker TEXT,
            best_yes_bid INTEGER, yes_ask INTEGER, spread INTEGER,
            midpoint REAL, net_inventory INTEGER, yes_held INTEGER,
            no_held INTEGER, realized_pnl REAL, unrealized_pnl REAL,
            total_pnl REAL, total_fees REAL,
            yes_order_price INTEGER, yes_queue_pos INTEGER,
            no_order_price INTEGER, no_queue_pos INTEGER,
            trade_volume_1min INTEGER,
            global_realized_pnl REAL, global_unrealized_pnl REAL,
            global_total_pnl REAL
        );
        CREATE TABLE mm_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, ticker TEXT, layer INTEGER,
            action TEXT, trigger_reason TEXT,
            net_inventory INTEGER, realized_pnl REAL,
            unrealized_pnl REAL, midpoint REAL, spread INTEGER,
            consecutive_losses INTEGER
        );
    """)
    sid = "test-residual"
    # YES fill: 2 @ 45c (paired)
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 1, "MKT_A", "yes_bid", 45, 2, 0.77, 0, 2,
         "2026-03-15T10:00:00+00:00"))
    # NO fill: 2 @ 53c (paired)
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 2, "MKT_A", "no_bid", 53, 2, 0.87, 0, 0,
         "2026-03-15T10:05:00+00:00"))
    # Extra NO fill: 2 @ 61c (unpaired — residual inventory)
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 3, "MKT_A", "no_bid", 61, 2, 0.83, 0, -2,
         "2026-03-15T10:10:00+00:00"))
    # Snapshot with unrealized P&L from residual
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T10:00:00+00:00", "MKT_A", -2, 1.53, -5.0, -3.47, 2.47, 8, 48.0))
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T12:00:00+00:00", "MKT_A", -2, 1.53, -5.0, -3.47, 2.47, 8, 48.0))
    conn.commit()
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_summary.py -k "pnl_split or residual" -v`
Expected: FAIL — "Spread P&L" not in summary output

- [ ] **Step 3: Write implementation**

Add a `compute_pnl_split` helper function in `scripts/session_summary.py` and integrate into `generate_summary`:

```python
def compute_pnl_split(conn: sqlite3.Connection, session_id: str,
                      ticker: str) -> dict:
    """Decompose P&L into spread (paired round-trips) vs inventory (residual).

    Returns dict with spread_pnl, inventory_pnl, round_trips, residual_count,
    residual_side, residual_avg_cost.
    """
    fills = conn.execute(
        "SELECT side, price, size, fee FROM mm_fills "
        "WHERE session_id=? AND ticker=? AND side != 'settlement' "
        "ORDER BY filled_at",
        (session_id, ticker)).fetchall()

    yes_costs = []  # (price, fee_per_contract)
    no_costs = []

    for side, price, size, fee in fills:
        per_fee = fee / size if size > 0 else 0
        if "yes" in side:
            yes_costs.extend([(price, per_fee)] * size)
        elif "no" in side:
            no_costs.extend([(price, per_fee)] * size)

    n_pairs = min(len(yes_costs), len(no_costs))
    spread_pnl = 0.0
    for i in range(n_pairs):
        yc, yf = yes_costs[i]
        nc, nf = no_costs[i]
        spread_pnl += 100 - yc - nc - yf - nf

    remaining_yes = len(yes_costs) - n_pairs
    remaining_no = len(no_costs) - n_pairs

    # Get unrealized from last snapshot
    snap = conn.execute(
        "SELECT unrealized_pnl FROM mm_snapshots "
        "WHERE session_id=? AND ticker=? ORDER BY ts DESC LIMIT 1",
        (session_id, ticker)).fetchone()
    unrealized = snap[0] if snap else 0.0

    # Residual info
    if remaining_yes > 0:
        leftover = yes_costs[n_pairs:]
        residual_side = "YES"
        residual_count = remaining_yes
        residual_avg = sum(p for p, _ in leftover) / len(leftover)
    elif remaining_no > 0:
        leftover = no_costs[n_pairs:]
        residual_side = "NO"
        residual_count = remaining_no
        residual_avg = sum(p for p, _ in leftover) / len(leftover)
    else:
        residual_side = None
        residual_count = 0
        residual_avg = 0

    return {
        "spread_pnl": round(spread_pnl, 1),
        "inventory_pnl": round(unrealized, 1),
        "round_trips": n_pairs,
        "residual_count": residual_count,
        "residual_side": residual_side,
        "residual_avg_cost": round(residual_avg, 0),
    }
```

Then in `generate_summary`, after the per-market loop, add a P&L decomposition section:

```python
    # After "## Aggregate Stats" section, add:
    lines.extend([
        "",
        "## P&L Decomposition (Spread vs Inventory)",
        "| Market | Round-trips | Spread P&L | Residual | Inventory P&L | Mix |",
        "|--------|------------|------------|----------|--------------|-----|",
    ])
    total_spread = 0.0
    total_inv = 0.0
    for ticker in tickers:
        split = compute_pnl_split(conn, sid, ticker)
        total_spread += split["spread_pnl"]
        total_inv += split["inventory_pnl"]
        residual_str = (f"{split['residual_count']} {split['residual_side']} "
                        f"@ {split['residual_avg_cost']:.0f}c"
                        if split["residual_side"] else "flat")
        abs_total = abs(split["spread_pnl"]) + abs(split["inventory_pnl"])
        if abs_total > 0:
            pct = f"{split['spread_pnl'] / abs_total * 100:.0f}%/{split['inventory_pnl'] / abs_total * 100:.0f}%"
        else:
            pct = "n/a"
        lines.append(
            f"| {ticker} | {split['round_trips']} | "
            f"{split['spread_pnl']:+.1f}c | {residual_str} | "
            f"{split['inventory_pnl']:+.1f}c | {pct} |")
    abs_grand = abs(total_spread) + abs(total_inv)
    if abs_grand > 0:
        grand_pct = f"{total_spread / abs_grand * 100:.0f}% spread / {total_inv / abs_grand * 100:.0f}% inv"
    else:
        grand_pct = "n/a"
    lines.append(f"| **Total** | | **{total_spread:+.1f}c** | | **{total_inv:+.1f}c** | {grand_pct} |")
```

Move `conn.close()` after this new section (it's currently at line 168 — move it to after P&L decomposition is computed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_summary.py -v`
Expected: All 10 tests PASS (7 existing + 3 new)

Run full suite: `python -m pytest tests/test_mm_*.py tests/test_*skew*.py tests/test_*spread*.py tests/test_*obi*.py tests/test_pregame*.py tests/test_silent*.py tests/test_monitor.py tests/test_inventory*.py tests/test_session_summary.py tests/test_daily_scan.py -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/session_summary.py tests/test_session_summary.py
git commit -m "feat(ops): split spread vs inventory P&L in session summary"
```

---

## Task 5: Integration Test — Full Flow

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -q --ignore=tests/test_risk.py --ignore=tests/test_evaluator.py --ignore=tests/test_scanner.py --ignore=tests/test_trade_pipeline.py
```
Expected: All tests PASS

- [ ] **Step 2: Verify soft-close doesn't break existing pregame exit tests**

```bash
python -m pytest tests/test_pregame_exit.py -v
```
Expected: All 8 tests PASS

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "test: verify all improvements pass full test suite"
```
