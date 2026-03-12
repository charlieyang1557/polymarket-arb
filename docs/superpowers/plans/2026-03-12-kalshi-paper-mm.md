# Kalshi Paper Trading Market Maker — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a paper trading market maker that simulates passive MM across 5 Kalshi markets with FIFO queue fill simulation, 4-layer risk management, and SQLite audit logging.

**Architecture:** Poll-based synchronous loop (10s/market). Each tick fetches orderbook + trades, drains simulated queue positions, detects fills, runs risk checks, and places/cancels simulated orders. Separate SQLite DB for all MM data with session_id isolation.

**Tech Stack:** Python 3.12, raw sqlite3, requests (via existing KalshiClient), python-dotenv

**Spec:** `docs/superpowers/specs/2026-03-12-kalshi-paper-mm-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/mm/__init__.py` | Package marker (empty) |
| `src/mm/state.py` | SimOrder, MarketState, GlobalState dataclasses + fee helpers |
| `src/mm/risk.py` | Action enum, Layers 1-4 check functions, action priority |
| `src/mm/db.py` | MMDatabase class — table creation, insert helpers, snapshot writes |
| `src/mm/engine.py` | MMEngine class — tick loop, fill simulation, quote management, pairing |
| `scripts/paper_mm.py` | CLI entry point, argparse, main loop, graceful shutdown |
| `tests/test_mm_state.py` | Tests for state, fees, unrealized P&L |
| `tests/test_mm_risk.py` | Tests for all 4 risk layers |
| `tests/test_mm_engine.py` | Tests for fill simulation, queue drain, pairing |

Note: DB logic gets its own file (`src/mm/db.py`) rather than being in engine.py to keep engine focused on trading logic. The spec said "MMDatabase helper class in engine.py" but separating is cleaner and the spec allows this flexibility.

---

## Chunk 1: Data Model & Fees

### Task 1: State dataclasses (`src/mm/state.py`)

**Files:**
- Create: `src/mm/__init__.py`
- Create: `src/mm/state.py`
- Create: `tests/test_mm_state.py`

- [ ] **Step 1: Write tests for fee calculations and unrealized P&L**

```python
# tests/test_mm_state.py
from src.mm.state import maker_fee_cents, taker_fee_cents, unrealized_pnl_cents

def test_maker_fee_at_26c():
    # Spec worked example: 2 contracts at 26c = 0.67c
    assert abs(maker_fee_cents(26, 2) - 0.6734) < 0.01

def test_maker_fee_at_69c():
    # Spec: 1 contract at 69c = 0.37c
    assert abs(maker_fee_cents(69, 1) - 0.3745) < 0.01

def test_taker_fee_at_26c():
    # Spec: 2 contracts at 26c = 2.69c
    assert abs(taker_fee_cents(26, 2) - 2.6936) < 0.01

def test_maker_fee_at_50c_maximum():
    # Max fee at P=0.50: 0.0175 * 1 * 0.5 * 0.5 * 100 = 0.4375c
    assert abs(maker_fee_cents(50, 1) - 0.4375) < 0.01

def test_unrealized_pnl_long_yes():
    # Holding 2 YES at costs [26, 28], best_yes_bid=29
    # Unrealized = (29-26) + (29-28) = 4 (conservative: bid not midpoint)
    yes_q = [26, 28]
    no_q = []
    assert unrealized_pnl_cents(yes_q, no_q, best_yes_bid=29, best_no_bid=69) == 4.0

def test_unrealized_pnl_long_no():
    # Holding 1 NO at cost [69], best_no_bid=70
    # Unrealized = 70-69 = 1
    yes_q = []
    no_q = [69]
    assert unrealized_pnl_cents(yes_q, no_q, best_yes_bid=26, best_no_bid=70) == 1.0

def test_unrealized_pnl_hedged():
    # Fully hedged: 2 YES + 2 NO, no unhedged tail
    assert unrealized_pnl_cents([26, 28], [69, 71],
                                best_yes_bid=29, best_no_bid=70) == 0.0

def test_unrealized_pnl_partial_hedge():
    # 3 YES + 1 NO: first pair hedged, 2 YES unhedged
    # Unhedged YES at costs [28, 30], best_yes_bid=31
    # Unrealized = (31-28) + (31-30) = 4
    assert unrealized_pnl_cents([26, 28, 30], [69],
                                best_yes_bid=31, best_no_bid=69) == 4.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mm_state.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement state.py**

```python
# src/mm/state.py
"""Data model for the paper market maker."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


def maker_fee_cents(price_cents: int, count: int) -> float:
    """Kalshi maker fee in cents. Formula: 0.0175 * count * P * (1-P) * 100."""
    p = price_cents / 100
    return 0.0175 * count * p * (1 - p) * 100


def taker_fee_cents(price_cents: int, count: int) -> float:
    """Kalshi taker fee in cents. Formula: 0.07 * count * P * (1-P) * 100."""
    p = price_cents / 100
    return 0.07 * count * p * (1 - p) * 100


def unrealized_pnl_cents(yes_queue: list[int], no_queue: list[int],
                         best_yes_bid: int, best_no_bid: int) -> float:
    """Conservative mark-to-market unrealized P&L for unhedged inventory.

    Uses exit prices (bids), NOT midpoint, to avoid phantom profits
    in wide-spread markets. YES valued at best_yes_bid, NO at best_no_bid.
    """
    if len(yes_queue) > len(no_queue):
        unhedged = yes_queue[len(no_queue):]
        return sum(best_yes_bid - cost for cost in unhedged)
    elif len(no_queue) > len(yes_queue):
        unhedged = no_queue[len(yes_queue):]
        return sum(best_no_bid - cost for cost in unhedged)
    return 0.0


@dataclass
class SimOrder:
    """A simulated resting order."""
    side: str           # "yes" or "no"
    price: int          # cents
    size: int
    remaining: int
    queue_pos: int      # contracts ahead of us
    placed_at: datetime
    last_drain_trade_id: str = ""  # per-order trade dedup for queue drain
    db_id: int | None = None  # mm_orders row id once persisted


@dataclass
class MarketState:
    """Per-market state for the paper MM."""
    ticker: str
    active: bool = True
    yes_order: SimOrder | None = None
    no_order: SimOrder | None = None
    yes_queue: list[int] = field(default_factory=list)
    no_queue: list[int] = field(default_factory=list)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees: float = 0.0
    last_seen_trade_id: str = ""
    consecutive_losses: int = 0
    paused_until: datetime | None = None
    midpoint_history: list[tuple[datetime, float]] = field(default_factory=list)
    last_api_success: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    trade_volume_1min: int = 0  # trades at our price level in last 60s
    oldest_fill_time: datetime | None = None  # for L2 time-based checks
    skew_activated_at: datetime | None = None  # when inventory skewing started

    @property
    def net_inventory(self) -> int:
        """Positive = long YES, negative = long NO."""
        return len(self.yes_queue) - len(self.no_queue)


@dataclass
class GlobalState:
    """Aggregate state across all markets."""
    markets: dict[str, MarketState] = field(default_factory=dict)
    start_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    session_id: str = ""
    db_error_count: int = 0
    peak_total_pnl: float = 0.0

    @property
    def total_realized_pnl(self) -> float:
        return sum(m.realized_pnl for m in self.markets.values())

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(m.unrealized_pnl for m in self.markets.values())

    @property
    def total_pnl(self) -> float:
        return self.total_realized_pnl + self.total_unrealized_pnl
```

- [ ] **Step 4: Create `src/mm/__init__.py`**

```python
# empty file
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_mm_state.py -v`
Expected: All 8 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/mm/__init__.py src/mm/state.py tests/test_mm_state.py
git commit -m "feat(mm): data model — SimOrder, MarketState, GlobalState, fee helpers"
```

---

## Chunk 2: Risk Management

### Task 2: Risk layer checks (`src/mm/risk.py`)

**Files:**
- Create: `src/mm/risk.py`
- Create: `tests/test_mm_risk.py`

- [ ] **Step 1: Write tests for risk actions**

```python
# tests/test_mm_risk.py
from datetime import datetime, timezone, timedelta
from src.mm.risk import Action, check_layer1, check_layer2, check_layer3, check_layer4, highest_priority
from src.mm.state import MarketState, SimOrder, GlobalState

def test_action_priority():
    assert highest_priority([Action.CONTINUE, Action.PAUSE_60S, Action.SKIP_TICK]) == Action.PAUSE_60S
    assert highest_priority([Action.CONTINUE]) == Action.CONTINUE
    assert highest_priority([Action.FULL_STOP, Action.CONTINUE]) == Action.FULL_STOP

# Layer 1
def test_l1_rejects_oversized():
    assert check_layer1(price=26, size=10, midpoint=28.0, max_size=5) is not None

def test_l1_rejects_fat_finger():
    # midpoint=28, 10% = 2.8, so price 32 is outside ±10%
    assert check_layer1(price=32, size=2, midpoint=28.0, max_size=5) is not None

def test_l1_accepts_valid():
    assert check_layer1(price=27, size=2, midpoint=28.0, max_size=5) is None

# Layer 2
def test_l2_continue_under_10():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 5  # net +5
    assert check_layer2(ms) == Action.CONTINUE

def test_l2_skew_11_to_20():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 15  # net +15
    assert check_layer2(ms) == Action.SKEW_QUOTES

def test_l2_aggress_after_skew_1h():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 15  # net +15
    ms.skew_activated_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=1)
    assert check_layer2(ms) == Action.AGGRESS_FLATTEN

def test_l2_stop_over_20():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 25  # net +25
    assert check_layer2(ms) == Action.STOP_AND_FLATTEN

def test_l2_skew_at_2h_old_position():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26]
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=3)
    assert check_layer2(ms) == Action.SKEW_QUOTES

def test_l2_force_close_at_4h_old_position():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26]
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=5)
    assert check_layer2(ms) == Action.FORCE_CLOSE

# Layer 3
def test_l3_daily_loss_full_stop():
    gs = GlobalState()
    ms = MarketState(ticker="X", realized_pnl=-600)  # > $5 loss
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.FULL_STOP

def test_l3_consecutive_losses_pause():
    gs = GlobalState()
    ms = MarketState(ticker="X", consecutive_losses=3)
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.PAUSE_30MIN

def test_l3_per_market_exit():
    gs = GlobalState()
    ms = MarketState(ticker="X", realized_pnl=-1100)
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.EXIT_MARKET

def test_l3_drawdown_triple_gate():
    gs = GlobalState(peak_total_pnl=200)
    ms = MarketState(ticker="X", realized_pnl=80)
    gs.markets["X"] = ms
    # peak=200, current=80, drawdown=120 > 50c, > 5% of 200
    assert check_layer3(ms, gs) == Action.FULL_STOP

def test_l3_drawdown_no_trigger_small_peak():
    gs = GlobalState(peak_total_pnl=50)  # peak < 100, gate 1 fails
    ms = MarketState(ticker="X", realized_pnl=0)
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.CONTINUE

# Layer 4
def test_l4_price_jump():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [
        (now - timedelta(seconds=60), 26.0),
        (now, 32.0),  # 6c jump
    ]
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.PAUSE_60S

def test_l4_crossed_book():
    ms = MarketState(ticker="X")
    assert check_layer4(ms, spread=-1, db_error_count=0) == Action.SKIP_TICK

def test_l4_db_errors():
    ms = MarketState(ticker="X")
    assert check_layer4(ms, spread=5, db_error_count=10) == Action.FULL_STOP
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mm_risk.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement risk.py**

```python
# src/mm/risk.py
"""Risk management layers 1-4 for the paper market maker."""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from src.mm.state import MarketState, GlobalState


class Action(IntEnum):
    """Risk actions ordered by priority (highest = most restrictive)."""
    CONTINUE = 0
    SKIP_TICK = 1
    PAUSE_30MIN = 2
    PAUSE_60S = 3
    SKEW_QUOTES = 4
    AGGRESS_FLATTEN = 5
    FORCE_CLOSE = 6
    STOP_AND_FLATTEN = 7
    CANCEL_ALL = 8
    EXIT_MARKET = 9
    FULL_STOP = 10


def highest_priority(actions: list[Action]) -> Action:
    return max(actions) if actions else Action.CONTINUE


# -- Layer 1: Per-Order Validation -----------------------------------------

def check_layer1(price: int, size: int, midpoint: float,
                 max_size: int = 5) -> str | None:
    """Returns rejection reason string, or None if valid."""
    if size > max_size:
        return f"size {size} > max {max_size}"
    if midpoint > 0 and abs(price - midpoint) > midpoint * 0.10:
        return f"price {price} outside ±10% of midpoint {midpoint:.1f}"
    return None


# -- Layer 2: Inventory Management ----------------------------------------

def check_layer2(ms: MarketState) -> Action:
    net = abs(ms.net_inventory)
    now = datetime.now(timezone.utc)

    # Time-based checks on oldest unhedged position
    if ms.oldest_fill_time:
        age = now - ms.oldest_fill_time
        if age > timedelta(hours=4):
            return Action.FORCE_CLOSE
        if age > timedelta(hours=2):
            return Action.SKEW_QUOTES

    if net > 20:
        return Action.STOP_AND_FLATTEN
    if net > 10:
        # Check if skewing has been active > 1 hour without reducing inventory
        if ms.skew_activated_at and \
           (now - ms.skew_activated_at) > timedelta(hours=1):
            return Action.AGGRESS_FLATTEN
        return Action.SKEW_QUOTES
    return Action.CONTINUE


# -- Layer 3: P&L Circuit Breakers ----------------------------------------

def check_layer3(ms: MarketState, gs: GlobalState) -> Action:
    # Collect all triggered actions, return highest priority.
    # This prevents early-return from masking higher-priority actions.
    actions = []

    # Daily loss across all markets
    if gs.total_realized_pnl < -500:
        actions.append(Action.FULL_STOP)

    # Drawdown triple-gate
    peak = gs.peak_total_pnl
    current = gs.total_pnl
    drawdown = peak - current
    if peak > 100 and drawdown > 50 and drawdown / peak > 0.05:
        actions.append(Action.FULL_STOP)

    # Per-market cumulative loss
    if ms.realized_pnl < -1000:
        actions.append(Action.EXIT_MARKET)

    # Consecutive losses
    if ms.consecutive_losses >= 3:
        actions.append(Action.PAUSE_30MIN)

    return highest_priority(actions) if actions else Action.CONTINUE


# -- Layer 4: System Risk -------------------------------------------------

def check_layer4(ms: MarketState, spread: int,
                 db_error_count: int) -> Action:
    # DB write failures
    if db_error_count >= 10:
        return Action.FULL_STOP

    # Crossed book
    if spread <= 0:
        return Action.SKIP_TICK

    # API disconnect (only relevant if we attempted calls)
    now = datetime.now(timezone.utc)
    if hasattr(ms, 'last_api_success'):
        if (now - ms.last_api_success) > timedelta(seconds=30):
            return Action.CANCEL_ALL

    # Price jump > 5c in last 60s
    if len(ms.midpoint_history) >= 2:
        oldest_entry = ms.midpoint_history[0]
        newest_entry = ms.midpoint_history[-1]
        time_diff = (newest_entry[0] - oldest_entry[0]).total_seconds()
        if time_diff <= 65 and abs(newest_entry[1] - oldest_entry[1]) > 5:
            return Action.PAUSE_60S

    return Action.CONTINUE
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mm_risk.py -v`
Expected: All 15 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mm/risk.py tests/test_mm_risk.py
git commit -m "feat(mm): risk management — Layers 1-4 with action priority"
```

---

## Chunk 3: Database Layer

### Task 3: MM Database (`src/mm/db.py`)

**Files:**
- Create: `src/mm/db.py`
- Create: `tests/test_mm_db.py`

- [ ] **Step 1: Write tests for DB operations**

```python
# tests/test_mm_db.py
import os, tempfile
from src.mm.db import MMDatabase

def test_create_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        # Tables should exist
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {t[0] for t in tables}
        assert "mm_orders" in names
        assert "mm_fills" in names
        assert "mm_snapshots" in names
        assert "mm_events" in names
        db.close()
    finally:
        os.unlink(path)

def test_insert_order():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        oid = db.insert_order("KXTEST", "yes", 26, 2, 2, 42, "resting",
                              "2026-03-12T00:00:00Z")
        assert oid > 0
        row = db.conn.execute("SELECT * FROM mm_orders WHERE id=?",
                              (oid,)).fetchone()
        assert row is not None
        db.close()
    finally:
        os.unlink(path)

def test_insert_fill():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        fid = db.insert_fill(order_id=None, ticker="KXTEST", side="yes_bid",
                             price=26, size=2, fee=0.67, is_taker=0,
                             inventory_after=2, filled_at="2026-03-12T00:01:00Z")
        assert fid > 0
        db.close()
    finally:
        os.unlink(path)

def test_insert_event():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        db.insert_event("2026-03-12T00:00:00Z", "KXTEST", 2,
                        "AGGRESS_FLATTEN", "net_inv=15 > 10",
                        net_inventory=15, realized_pnl=0,
                        unrealized_pnl=-5.0, midpoint=28.0,
                        spread=5, consecutive_losses=0)
        rows = db.conn.execute("SELECT * FROM mm_events").fetchall()
        assert len(rows) == 1
        db.close()
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mm_db.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement db.py**

```python
# src/mm/db.py
"""SQLite database for the paper market maker."""

import sqlite3
from pathlib import Path


class MMDatabase:
    """Manages the MM-specific SQLite database."""

    def __init__(self, db_path: str, session_id: str):
        self.session_id = session_id
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS mm_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price INTEGER NOT NULL,
                size INTEGER NOT NULL,
                remaining INTEGER NOT NULL,
                queue_pos_initial INTEGER,
                status TEXT NOT NULL,
                placed_at TEXT NOT NULL,
                filled_at TEXT,
                cancelled_at TEXT,
                cancel_reason TEXT,
                time_in_queue_s REAL
            );
            CREATE TABLE IF NOT EXISTS mm_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                order_id INTEGER REFERENCES mm_orders(id),
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price INTEGER NOT NULL,
                size INTEGER NOT NULL,
                fee REAL NOT NULL,
                is_taker INTEGER NOT NULL,
                inventory_after INTEGER,
                pair_id INTEGER,
                pair_pnl REAL,
                filled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mm_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                ticker TEXT NOT NULL,
                best_yes_bid INTEGER,
                yes_ask INTEGER,
                spread INTEGER,
                midpoint REAL,
                net_inventory INTEGER,
                yes_held INTEGER,
                no_held INTEGER,
                realized_pnl REAL,
                unrealized_pnl REAL,
                total_pnl REAL,
                total_fees REAL,
                yes_order_price INTEGER,
                yes_queue_pos INTEGER,
                no_order_price INTEGER,
                no_queue_pos INTEGER,
                trade_volume_1min INTEGER,
                global_realized_pnl REAL,
                global_unrealized_pnl REAL,
                global_total_pnl REAL
            );
            CREATE TABLE IF NOT EXISTS mm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                ticker TEXT,
                layer INTEGER NOT NULL,
                action TEXT NOT NULL,
                trigger_reason TEXT NOT NULL,
                net_inventory INTEGER,
                realized_pnl REAL,
                unrealized_pnl REAL,
                midpoint REAL,
                spread INTEGER,
                consecutive_losses INTEGER
            );
        """)
        self.conn.commit()

    def insert_order(self, ticker, side, price, size, remaining,
                     queue_pos_initial, status, placed_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO mm_orders (session_id, ticker, side, price, size, "
            "remaining, queue_pos_initial, status, placed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.session_id, ticker, side, price, size, remaining,
             queue_pos_initial, status, placed_at))
        self.conn.commit()
        return cur.lastrowid

    def update_order(self, order_id: int, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [order_id]
        self.conn.execute(f"UPDATE mm_orders SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def insert_fill(self, order_id, ticker, side, price, size, fee,
                    is_taker, inventory_after, filled_at,
                    pair_id=None, pair_pnl=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
            "size, fee, is_taker, inventory_after, pair_id, pair_pnl, "
            "filled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, order_id, ticker, side, price, size, fee,
             is_taker, inventory_after, pair_id, pair_pnl, filled_at))
        self.conn.commit()
        return cur.lastrowid

    def insert_snapshot(self, **kwargs):
        kwargs["session_id"] = self.session_id
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" * len(kwargs))
        self.conn.execute(
            f"INSERT INTO mm_snapshots ({cols}) VALUES ({placeholders})",
            list(kwargs.values()))
        self.conn.commit()

    def insert_event(self, ts, ticker, layer, action, trigger_reason, **kw):
        self.conn.execute(
            "INSERT INTO mm_events (session_id, ts, ticker, layer, action, "
            "trigger_reason, net_inventory, realized_pnl, unrealized_pnl, "
            "midpoint, spread, consecutive_losses) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, ts, ticker, layer, action, trigger_reason,
             kw.get("net_inventory"), kw.get("realized_pnl"),
             kw.get("unrealized_pnl"), kw.get("midpoint"),
             kw.get("spread"), kw.get("consecutive_losses")))
        self.conn.commit()

    def close(self):
        self.conn.close()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mm_db.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mm/db.py tests/test_mm_db.py
git commit -m "feat(mm): SQLite database — 4 tables with session_id isolation"
```

---

## Chunk 4: Engine — Fill Simulation & Quoting

### Task 4: Core engine (`src/mm/engine.py`)

**Files:**
- Create: `src/mm/engine.py`
- Create: `tests/test_mm_engine.py`

- [ ] **Step 1: Write tests for fill simulation logic**

```python
# tests/test_mm_engine.py
from datetime import datetime, timezone
from src.mm.engine import drain_queue, process_fills, pair_off_inventory
from src.mm.state import SimOrder, MarketState

def test_drain_queue_yes_bid():
    """Trades at or below our YES bid price drain the queue."""
    order = SimOrder(side="yes", price=26, size=2, remaining=2,
                     queue_pos=42,
                     placed_at=datetime.now(timezone.utc))
    # Simulated trades: 15 contracts at yes_price <= 26
    trades = [{"trade_id": "t1", "count_fp": "15.0",
               "yes_price_dollars": "0.2500",
               "created_time": datetime.now(timezone.utc).isoformat()}]
    drain = drain_queue(order, trades)
    assert drain == 15
    # queue_pos should be updated by caller

def test_drain_queue_no_bid():
    """NO bid drains from trades where (100 - yes_price) <= NO bid price."""
    order = SimOrder(side="no", price=69, size=2, remaining=2,
                     queue_pos=30,
                     placed_at=datetime.now(timezone.utc))
    # Trade at yes_price=30c -> no_price=70c. 70 > 69, does NOT drain.
    trades_no_drain = [{"trade_id": "t1", "count_fp": "10.0",
                        "yes_price_dollars": "0.3000",
                        "created_time": datetime.now(timezone.utc).isoformat()}]
    assert drain_queue(order, trades_no_drain) == 0

    # Trade at yes_price=32c -> no_price=68c. 68 <= 69, drains.
    trades_drain = [{"trade_id": "t2", "count_fp": "10.0",
                     "yes_price_dollars": "0.3200",
                     "created_time": datetime.now(timezone.utc).isoformat()}]
    assert drain_queue(order, trades_drain) == 10

def test_process_fills_full():
    """Queue drains past zero -> fill our order."""
    order = SimOrder(side="yes", price=26, size=2, remaining=2,
                     queue_pos=5,
                     placed_at=datetime.now(timezone.utc))
    filled = process_fills(order, drain=8)
    assert filled == 2  # min(remaining=2, max(0, 8-5)=3) -> 2
    assert order.remaining == 0
    assert order.queue_pos == 0

def test_process_fills_partial():
    """Queue partially drains -> partial fill."""
    order = SimOrder(side="yes", price=26, size=5, remaining=5,
                     queue_pos=2,
                     placed_at=datetime.now(timezone.utc))
    filled = process_fills(order, drain=4)
    assert filled == 2  # min(5, max(0, 4-2)) = 2
    assert order.remaining == 3
    assert order.queue_pos == 0

def test_process_fills_no_fill():
    """Drain doesn't reach our queue position."""
    order = SimOrder(side="yes", price=26, size=2, remaining=2,
                     queue_pos=42,
                     placed_at=datetime.now(timezone.utc))
    filled = process_fills(order, drain=10)
    assert filled == 0
    assert order.queue_pos == 32

def test_pair_off_inventory():
    """Matched YES+NO pairs settle at 100c."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [26, 28]
    ms.no_queue = [69]
    # Should pair first YES(26) + first NO(69)
    pairs = pair_off_inventory(ms)
    assert len(pairs) == 1
    gross = 100 - 26 - 69  # = 5c
    assert pairs[0]["gross_pnl"] == gross
    assert len(ms.yes_queue) == 1  # [28] remains
    assert len(ms.no_queue) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mm_engine.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement engine.py core functions**

The engine module is the largest (~180 lines). It contains:

1. `drain_queue(order, trades)` — count trade volume that drains our queue
2. `process_fills(order, drain)` — apply drain to order, return fill count
3. `pair_off_inventory(ms)` — settle matched YES+NO pairs
4. `MMEngine` class with `tick_one_market()` method

```python
# src/mm/engine.py
"""Core engine for the paper market maker."""

from __future__ import annotations
import logging
import sys
import time
import os
from datetime import datetime, timezone, timedelta
from src.mm.state import (
    SimOrder, MarketState, GlobalState,
    maker_fee_cents, taker_fee_cents, unrealized_pnl_cents,
)
from src.mm.risk import Action, check_layer1, check_layer2, check_layer3, check_layer4, highest_priority
from src.mm.db import MMDatabase
from src.kalshi_client import KalshiClient

import requests as _requests  # for discord

logger = logging.getLogger(__name__)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")


# -- Fill simulation helpers -----------------------------------------------

def drain_queue(order: SimOrder, trades: list[dict]) -> int:
    """Count trade volume that drains queue ahead of our order.

    YES bid at P: drain from trades where yes_price_cents <= P
    NO bid at P:  drain from trades where (100 - yes_price_cents) <= P
    """
    total = 0
    for t in trades:
        count = float(t.get("count_fp", 0) or 0)
        yes_price_cents = round(
            float(t.get("yes_price_dollars", 0) or 0) * 100)

        if order.side == "yes":
            if yes_price_cents <= order.price:
                total += count
        else:  # "no"
            no_price_cents = 100 - yes_price_cents
            if no_price_cents <= order.price:
                total += count
    return int(total)


def process_fills(order: SimOrder, drain: int) -> int:
    """Apply drain to order queue position. Returns number filled."""
    if order.queue_pos > 0:
        if drain <= order.queue_pos:
            order.queue_pos -= drain
            return 0
        overflow = drain - order.queue_pos
        order.queue_pos = 0
        filled = min(order.remaining, overflow)
    else:
        filled = min(order.remaining, drain)

    order.remaining -= filled
    return int(filled)


def pair_off_inventory(ms: MarketState) -> list[dict]:
    """Settle matched YES+NO pairs. Returns list of pair results."""
    pairs = []
    while ms.yes_queue and ms.no_queue:
        yes_cost = ms.yes_queue.pop(0)
        no_cost = ms.no_queue.pop(0)
        gross = 100 - yes_cost - no_cost
        pairs.append({
            "yes_cost": yes_cost, "no_cost": no_cost,
            "gross_pnl": gross,
        })
    return pairs


# -- Discord ---------------------------------------------------------------

def discord_notify(msg: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        _requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
    except Exception:
        pass


# -- Engine ----------------------------------------------------------------

class MMEngine:
    """Runs the paper market making simulation."""

    def __init__(self, client: KalshiClient, db: MMDatabase,
                 global_state: GlobalState, order_size: int = 2):
        self.client = client
        self.db = db
        self.gs = global_state
        self.order_size = order_size
        self.tick_count = 0  # per-market tick counter (for snapshot every 6th)

    def tick_one_market(self, ms: MarketState):
        """Execute one tick cycle for a single market."""
        now = datetime.now(timezone.utc)

        # Check pause
        if ms.paused_until and now < ms.paused_until:
            return

        # -- 1. Fetch book + trades --
        try:
            book_data = self.client.get_orderbook(ms.ticker, depth=20)
            trade_data = self.client.get_trades(ms.ticker, limit=500)
            ms.last_api_success = now
        except _requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code in (401, 403, 404):
                self._log_event(ms, 4, Action.EXIT_MARKET,
                                f"fatal HTTP {code}")
                ms.active = False
                return
            self._log_event(ms, 4, Action.SKIP_TICK,
                            f"transient HTTP {code}")
            return
        except Exception as e:
            self._log_event(ms, 4, Action.SKIP_TICK, f"error: {e}")
            return

        # Parse book
        book = book_data.get("orderbook", book_data)
        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])
        if not yes_bids or not no_bids:
            return

        best_yes_bid = yes_bids[-1][0]
        best_no_bid = no_bids[-1][0]
        yes_ask = 100 - best_no_bid
        spread = yes_ask - best_yes_bid
        midpoint = (best_yes_bid + yes_ask) / 2

        # Update midpoint history (keep last 7 entries ~70s)
        ms.midpoint_history.append((now, midpoint))
        if len(ms.midpoint_history) > 7:
            ms.midpoint_history.pop(0)

        # -- 2. Layer 4 system checks --
        l4 = check_layer4(ms, spread, self.gs.db_error_count)
        if l4 != Action.CONTINUE:
            self._log_event(ms, 4, l4,
                            f"spread={spread} mid={midpoint:.1f}")
            if l4 == Action.PAUSE_60S:
                ms.paused_until = now + timedelta(seconds=60)
            elif l4 >= Action.CANCEL_ALL:
                self._cancel_orders(ms, "risk_l4")
                if l4 == Action.FULL_STOP:
                    for m in self.gs.markets.values():
                        m.active = False
                else:
                    ms.active = False
            return

        # -- 3. Filter new trades & drain queues --
        all_trades = trade_data.get("trades", [])
        new_trades = [t for t in all_trades
                      if t.get("trade_id", "") > ms.last_seen_trade_id]
        if new_trades:
            ms.last_seen_trade_id = max(
                t.get("trade_id", "") for t in new_trades)

        # Filter trades after order placement
        for order in (ms.yes_order, ms.no_order):
            if order is None or order.remaining <= 0:
                continue
            relevant = [t for t in new_trades
                        if t.get("created_time", "") >=
                        order.placed_at.isoformat()]
            d = drain_queue(order, relevant)
            if d > 0:
                filled = process_fills(order, d)
                if filled > 0:
                    self._record_fill(ms, order, filled, midpoint)

        # Track trade volume at our price levels for snapshot
        ms.trade_volume_1min = sum(
            int(float(t.get("count_fp", 0) or 0))
            for t in new_trades)

        # -- 4. Pair off matched inventory --
        pairs = pair_off_inventory(ms)
        for p in pairs:
            # Fees already deducted at fill time. Gross P&L from pairing.
            ms.realized_pnl += p["gross_pnl"]
            if p["gross_pnl"] < 0:
                ms.consecutive_losses += 1
            else:
                ms.consecutive_losses = 0
        # Reset oldest_fill_time if inventory fully paired off
        if not ms.yes_queue and not ms.no_queue:
            ms.oldest_fill_time = None
            ms.skew_activated_at = None

        # Update unrealized (conservative: use bid prices, not midpoint)
        ms.unrealized_pnl = unrealized_pnl_cents(
            ms.yes_queue, ms.no_queue, best_yes_bid, best_no_bid)

        # Update peak
        total = self.gs.total_pnl
        if total > self.gs.peak_total_pnl:
            self.gs.peak_total_pnl = total

        # -- 5. Risk checks (layers 2-3) --
        actions = [Action.CONTINUE]
        l2 = check_layer2(ms)
        if l2 != Action.CONTINUE:
            actions.append(l2)
            self._log_event(ms, 2, l2, f"net_inv={ms.net_inventory}")
        l3 = check_layer3(ms, self.gs)
        if l3 != Action.CONTINUE:
            actions.append(l3)
            self._log_event(ms, 3, l3,
                            f"rpnl={ms.realized_pnl:.1f} "
                            f"consec={ms.consecutive_losses}")

        action = highest_priority(actions)

        if action == Action.FULL_STOP:
            for m in self.gs.markets.values():
                self._cancel_orders(m, "full_stop")
                m.active = False
            return
        if action == Action.EXIT_MARKET:
            self._cancel_orders(ms, "exit_market")
            ms.active = False
            return
        if action == Action.PAUSE_30MIN:
            ms.paused_until = now + timedelta(minutes=30)
            self._cancel_orders(ms, "pause_30min")
            return
        if action in (Action.STOP_AND_FLATTEN, Action.FORCE_CLOSE):
            self._cancel_orders(ms, "flatten")
            self._aggress_flatten(ms, best_yes_bid, yes_ask,
                                  best_no_bid, midpoint)
            return
        if action == Action.AGGRESS_FLATTEN:
            self._aggress_flatten(ms, best_yes_bid, yes_ask,
                                  best_no_bid, midpoint)

        # Track skew activation for 1-hour escalation
        if action == Action.SKEW_QUOTES:
            if ms.skew_activated_at is None:
                ms.skew_activated_at = now
        elif abs(ms.net_inventory) <= 10:
            ms.skew_activated_at = None  # reset when inventory normalizes

        # -- 6. Place/update simulated orders --
        if action <= Action.AGGRESS_FLATTEN:
            skew = action == Action.SKEW_QUOTES
            self._manage_quotes(ms, best_yes_bid, best_no_bid,
                                yes_ask, midpoint, skew=skew)

        # -- 7. Snapshot every 6th tick (~60s) --
        self.tick_count += 1
        if self.tick_count % 6 == 0:
            self._write_snapshot(ms, best_yes_bid, yes_ask, spread,
                                 midpoint)

        # -- 8. Check market resolution (every 6th tick) --
        if self.tick_count % 6 == 0:
            self._check_resolution(ms, midpoint)

        # Terminal output
        q_yes = ms.yes_order.queue_pos if ms.yes_order else "-"
        q_no = ms.no_order.queue_pos if ms.no_order else "-"
        short = ms.ticker.replace("KXGREENLAND", "GRNLND").replace(
            "KXTRUMPREMOVE", "RMVTRMP").replace(
            "KXGREENLANDPRICE-29JAN21-NOACQ", "GRNLND-NO").replace(
            "KXVPRESNOMR-28-MR", "RUBIOVP").replace(
            "KXINSURRECTION-29-27", "INSURRCT")
        ts = now.strftime("%H:%M:%S")
        print(f"  [{ts}] {short:12s} mid={midpoint:.0f}c sprd={spread} "
              f"q_yes={q_yes} q_no={q_no} inv={ms.net_inventory} "
              f"pnl={ms.realized_pnl:.1f}c")

    # -- Internal helpers --------------------------------------------------

    def _record_fill(self, ms: MarketState, order: SimOrder,
                     filled: int, midpoint: float):
        """Record a simulated maker fill."""
        now = datetime.now(timezone.utc)
        fee = maker_fee_cents(order.price, filled)
        ms.total_fees += fee
        ms.realized_pnl -= fee  # fees reduce P&L immediately

        side_str = f"{order.side}_bid"
        if order.side == "yes":
            ms.yes_queue.extend([order.price] * filled)
        else:
            ms.no_queue.extend([order.price] * filled)

        # Track oldest fill time for L2 time-based checks
        if ms.oldest_fill_time is None:
            ms.oldest_fill_time = now

        inv = ms.net_inventory
        queue_time = (now - order.placed_at).total_seconds()

        try:
            fill_id = self.db.insert_fill(
                order_id=order.db_id, ticker=ms.ticker, side=side_str,
                price=order.price, size=filled, fee=fee, is_taker=0,
                inventory_after=inv, filled_at=now.isoformat())
            if order.db_id:
                updates = {"remaining": order.remaining,
                           "time_in_queue_s": queue_time}
                if order.remaining == 0:
                    updates["status"] = "filled"
                    updates["filled_at"] = now.isoformat()
                else:
                    updates["status"] = "partial"
                self.db.update_order(order.db_id, **updates)
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1
            print(f"  DB ERROR: {e}", file=sys.stderr)

        tag = "MAKER"
        print(f"  >>> FILL [{tag}] {side_str} {filled}@{order.price}c "
              f"fee={fee:.2f}c inv={inv} pnl={ms.realized_pnl:.1f}c "
              f"queue_time={queue_time:.0f}s")
        discord_notify(
            f"**Paper MM Fill** [{tag}] {ms.ticker} {side_str} "
            f"{filled}@{order.price}c | inv={inv} | "
            f"pnl={ms.realized_pnl:.1f}c")

    def _aggress_flatten(self, ms: MarketState, best_yes_bid: int,
                         yes_ask: int, best_no_bid: int,
                         midpoint: float):
        """Cross the spread to reduce inventory."""
        now = datetime.now(timezone.utc)
        net = ms.net_inventory
        if net == 0:
            return

        if net > 0:
            # Long YES -> buy NO to flatten
            price = 100 - best_yes_bid  # NO ask
            side_str = "no_aggress"
            size = min(self.order_size, abs(net))
            fee = taker_fee_cents(price, size)
            ms.no_queue.extend([price] * size)
        else:
            # Long NO -> buy YES to flatten
            price = yes_ask
            side_str = "yes_aggress"
            size = min(self.order_size, abs(net))
            fee = taker_fee_cents(price, size)
            ms.yes_queue.extend([price] * size)

        ms.total_fees += fee
        ms.realized_pnl -= fee
        inv = ms.net_inventory

        try:
            self.db.insert_fill(
                order_id=None, ticker=ms.ticker, side=side_str,
                price=price, size=size, fee=fee, is_taker=1,
                inventory_after=inv, filled_at=now.isoformat())
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1
            print(f"  DB ERROR: {e}", file=sys.stderr)

        print(f"  >>> FILL [TAKER] {side_str} {size}@{price}c "
              f"fee={fee:.2f}c inv={inv} pnl={ms.realized_pnl:.1f}c")
        discord_notify(
            f"**Paper MM Aggress** {ms.ticker} {side_str} "
            f"{size}@{price}c | inv={inv}")

    def _manage_quotes(self, ms: MarketState, best_yes_bid: int,
                       best_no_bid: int, yes_ask: int, midpoint: float,
                       skew: bool = False):
        """Place or update simulated resting orders.

        If skew=True (inventory > 10), adjust prices to attract
        offsetting flow rather than crossing the spread.
        """
        now = datetime.now(timezone.utc)
        net = ms.net_inventory  # positive = long YES

        for side, best_bid in [("yes", best_yes_bid), ("no", best_no_bid)]:
            order = ms.yes_order if side == "yes" else ms.no_order
            quote_price = best_bid

            # Inventory skewing: adjust quotes to attract offsetting flow
            if skew and net != 0:
                if net > 0:
                    # Long YES: lower YES bid (buy less), lower NO bid (attract NO sellers)
                    if side == "yes":
                        quote_price = max(1, best_bid - 2)  # less aggressive
                    else:
                        quote_price = max(1, best_bid - 1)  # more attractive
                else:
                    # Long NO: lower NO bid (buy less), lower YES bid (attract YES sellers)
                    if side == "no":
                        quote_price = max(1, best_bid - 2)
                    else:
                        quote_price = max(1, best_bid - 1)

            # Requote if order is stale (>2c from target price)
            if order and abs(order.price - quote_price) > 2:
                self._cancel_order(ms, side, "requote")
                order = None

            if order is None or order.remaining <= 0:
                # Layer 1 validation
                rejection = check_layer1(quote_price, self.order_size, midpoint)
                if rejection:
                    continue

                # Get depth at this price for queue position
                queue_pos = 50  # conservative default (fixed in Task 6)

                new_order = SimOrder(
                    side=side, price=quote_price, size=self.order_size,
                    remaining=self.order_size, queue_pos=queue_pos,
                    placed_at=now)

                try:
                    db_id = self.db.insert_order(
                        ms.ticker, side, quote_price, self.order_size,
                        self.order_size, queue_pos, "resting",
                        now.isoformat())
                    new_order.db_id = db_id
                    self.gs.db_error_count = 0
                except Exception as e:
                    self.gs.db_error_count += 1
                    print(f"  DB ERROR: {e}", file=sys.stderr)

                if side == "yes":
                    ms.yes_order = new_order
                else:
                    ms.no_order = new_order

    def _cancel_orders(self, ms: MarketState, reason: str):
        """Cancel all resting orders for a market."""
        for side in ("yes", "no"):
            self._cancel_order(ms, side, reason)

    def _cancel_order(self, ms: MarketState, side: str, reason: str):
        """Cancel one resting order."""
        order = ms.yes_order if side == "yes" else ms.no_order
        if order is None:
            return
        now = datetime.now(timezone.utc)
        if order.db_id:
            try:
                self.db.update_order(order.db_id,
                                     status="cancelled",
                                     cancelled_at=now.isoformat(),
                                     cancel_reason=reason)
                self.gs.db_error_count = 0
            except Exception as e:
                self.gs.db_error_count += 1
        if side == "yes":
            ms.yes_order = None
        else:
            ms.no_order = None

    def _log_event(self, ms: MarketState, layer: int,
                   action: Action, reason: str):
        """Log a risk event to DB and terminal."""
        now = datetime.now(timezone.utc)
        mid = (ms.midpoint_history[-1][1]
               if ms.midpoint_history else 0)
        print(f"  !!! RISK [L{layer}] {action.name}: {reason}")
        try:
            self.db.insert_event(
                now.isoformat(), ms.ticker, layer, action.name, reason,
                net_inventory=ms.net_inventory,
                realized_pnl=ms.realized_pnl,
                unrealized_pnl=ms.unrealized_pnl,
                midpoint=mid, spread=0,
                consecutive_losses=ms.consecutive_losses)
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1

        if layer >= 2:
            discord_notify(
                f"**Paper MM Risk** [{action.name}] {ms.ticker}: {reason}")

    def _write_snapshot(self, ms: MarketState, best_yes_bid: int,
                        yes_ask: int, spread: int, midpoint: float):
        """Write periodic state snapshot."""
        now = datetime.now(timezone.utc)
        try:
            self.db.insert_snapshot(
                ts=now.isoformat(), ticker=ms.ticker,
                best_yes_bid=best_yes_bid, yes_ask=yes_ask,
                spread=spread, midpoint=midpoint,
                net_inventory=ms.net_inventory,
                yes_held=len(ms.yes_queue),
                no_held=len(ms.no_queue),
                realized_pnl=ms.realized_pnl,
                unrealized_pnl=ms.unrealized_pnl,
                total_pnl=ms.realized_pnl + ms.unrealized_pnl,
                total_fees=ms.total_fees,
                yes_order_price=(ms.yes_order.price
                                 if ms.yes_order else None),
                yes_queue_pos=(ms.yes_order.queue_pos
                               if ms.yes_order else None),
                no_order_price=(ms.no_order.price
                                if ms.no_order else None),
                no_queue_pos=(ms.no_order.queue_pos
                              if ms.no_order else None),
                trade_volume_1min=ms.trade_volume_1min,
                global_realized_pnl=self.gs.total_realized_pnl,
                global_unrealized_pnl=self.gs.total_unrealized_pnl,
                global_total_pnl=self.gs.total_pnl)
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1
            print(f"  DB ERROR: {e}", file=sys.stderr)

    def _check_resolution(self, ms: MarketState, midpoint: float):
        """Check if market has resolved (once per minute)."""
        try:
            data = self.client.get_market(ms.ticker)
            market = data.get("market", data)
            result = market.get("result", "")
            if result in ("yes", "no"):
                self._settle_market(ms, result)
        except Exception:
            pass  # non-critical, skip silently

    def _settle_market(self, ms: MarketState, result: str):
        """Settle all inventory on market resolution."""
        now = datetime.now(timezone.utc)
        # Settle YES inventory
        for cost in ms.yes_queue:
            settle_price = 100 if result == "yes" else 0
            pnl = settle_price - cost
            ms.realized_pnl += pnl
            self.db.insert_fill(
                order_id=None, ticker=ms.ticker, side="settlement",
                price=settle_price, size=1, fee=0, is_taker=0,
                inventory_after=0, filled_at=now.isoformat(),
                pair_pnl=pnl)
        # Settle NO inventory
        for cost in ms.no_queue:
            settle_price = 100 if result == "no" else 0
            pnl = settle_price - cost
            ms.realized_pnl += pnl
            self.db.insert_fill(
                order_id=None, ticker=ms.ticker, side="settlement",
                price=settle_price, size=1, fee=0, is_taker=0,
                inventory_after=0, filled_at=now.isoformat(),
                pair_pnl=pnl)

        ms.yes_queue.clear()
        ms.no_queue.clear()
        ms.active = False
        self._cancel_orders(ms, "market_resolved")
        self._log_event(ms, 4, Action.EXIT_MARKET,
                        f"market resolved: {result}")
        print(f"  *** MARKET RESOLVED: {ms.ticker} -> {result}")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mm_engine.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/mm/engine.py tests/test_mm_engine.py
git commit -m "feat(mm): engine — fill simulation, queue drain, pairing, quoting"
```

---

## Chunk 5: Entry Point & Integration Test

### Task 5: CLI entry point (`scripts/paper_mm.py`)

**Files:**
- Create: `scripts/paper_mm.py`

- [ ] **Step 1: Implement paper_mm.py**

```python
#!/usr/bin/env python3
"""
Paper trading market maker for Kalshi.

Usage:
    python scripts/paper_mm.py                            # all Tier 1, 48h
    python scripts/paper_mm.py --tickers KXGREENLAND-29   # single market
    python scripts/paper_mm.py --duration 300             # 5 min test
    python scripts/paper_mm.py --size 3 --interval 15     # custom params
"""

import argparse
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.kalshi_client import KalshiClient, PROD_BASE
from src.mm.state import MarketState, GlobalState
from src.mm.engine import MMEngine, discord_notify
from src.mm.db import MMDatabase

load_dotenv()

DEFAULT_TICKERS = [
    "KXGREENLAND-29",
    "KXTRUMPREMOVE",
    "KXGREENLANDPRICE-29JAN21-NOACQ",
    "KXVPRESNOMR-28-MR",
    "KXINSURRECTION-29-27",
]


def main():
    parser = argparse.ArgumentParser(description="Paper trading market maker")
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS),
                        help="Comma-separated market tickers")
    parser.add_argument("--duration", type=int, default=172800,
                        help="Seconds to run (default: 48h)")
    parser.add_argument("--size", type=int, default=2,
                        help="Contracts per order (default: 2)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Seconds between ticks per market (default: 10)")
    parser.add_argument("--db-path", default="data/mm_paper.db")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH in .env")
        sys.exit(1)

    tickers = [t.strip() for t in args.tickers.split(",")]
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + \
                 uuid.uuid4().hex[:6]

    client = KalshiClient(api_key, pk_path, PROD_BASE)
    db = MMDatabase(args.db_path, session_id)
    gs = GlobalState(session_id=session_id)

    for ticker in tickers:
        gs.markets[ticker] = MarketState(ticker=ticker)

    engine = MMEngine(client, db, gs, order_size=args.size)

    # Graceful shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        print("\nShutting down gracefully...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Header
    n = len(tickers)
    print(f"Paper MM | {n} markets | {args.size} contracts | "
          f"{args.interval}s interval")
    print(f"Session: {session_id}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()} | "
          f"Duration: {args.duration}s | DB: {args.db_path}")
    print("-" * 70)

    discord_notify(
        f"**Paper MM Started** | {n} markets | session={session_id}")

    active_tickers = list(tickers)
    sleep_time = args.interval / max(len(active_tickers), 1)
    start = time.time()
    cycle = 0

    try:
        while not shutdown and (time.time() - start) < args.duration:
            active_tickers = [t for t in tickers
                              if gs.markets[t].active]
            if not active_tickers:
                print("All markets inactive. Stopping.")
                break

            for i, ticker in enumerate(active_tickers):
                if shutdown:
                    break
                # Stagger: only tick this market on its turn
                if cycle % len(active_tickers) != i:
                    continue
                ms = gs.markets[ticker]
                try:
                    engine.tick_one_market(ms)
                except Exception as e:
                    print(f"  UNEXPECTED ERROR on {ticker}: {e}",
                          file=sys.stderr)
                    # Per spec: unexpected error -> cancel all orders
                    engine._cancel_orders(ms, f"unexpected_error: {e}")

            cycle += 1
            time.sleep(sleep_time)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr)

    # Shutdown: cancel orders and write final snapshots
    for ms in gs.markets.values():
        engine._cancel_orders(ms, "shutdown")
        # Write final snapshot for each market
        if ms.midpoint_history:
            mid = ms.midpoint_history[-1][1]
            best_yb = int(mid - 2)  # approximate from last midpoint
            y_ask = int(mid + 2)
            engine._write_snapshot(ms, best_yb, y_ask,
                                   y_ask - best_yb, mid)

    # Summary
    elapsed = time.time() - start
    print(f"\n{'=' * 70}")
    print("SESSION SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Duration:           {elapsed/3600:.1f}h")
    print(f"  Session:            {session_id}")
    for ticker, ms in gs.markets.items():
        print(f"\n  {ticker}:")
        print(f"    Realized P&L:     {ms.realized_pnl:.1f}c")
        print(f"    Unrealized P&L:   {ms.unrealized_pnl:.1f}c")
        print(f"    Total fees:       {ms.total_fees:.1f}c")
        print(f"    Net inventory:    {ms.net_inventory}")
        print(f"    Active:           {ms.active}")

    print(f"\n  GLOBAL:")
    print(f"    Total realized:   {gs.total_realized_pnl:.1f}c")
    print(f"    Total unrealized: {gs.total_unrealized_pnl:.1f}c")
    print(f"    Total P&L:        {gs.total_pnl:.1f}c")
    print(f"    Peak P&L:         {gs.peak_total_pnl:.1f}c")
    print(f"    DB:               {args.db_path}")

    discord_notify(
        f"**Paper MM Ended** | {elapsed/3600:.1f}h | "
        f"pnl={gs.total_pnl:.1f}c | session={session_id}")

    db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/paper_mm.py
git commit -m "feat(mm): CLI entry point with graceful shutdown and session summary"
```

### Task 6: Fix queue_pos initialization from actual book depth

**Files:**
- Modify: `src/mm/engine.py` — `_manage_quotes` method

The current implementation uses `queue_pos = 50` as a conservative default. Fix this to use the actual book depth from the already-fetched orderbook data.

- [ ] **Step 1: Pass book data through tick to _manage_quotes**

Refactor `tick_one_market` to pass the parsed `yes_bids` and `no_bids` lists into `_manage_quotes`. Then in `_manage_quotes`, compute actual depth:

```python
# In _manage_quotes, replace queue_pos = 50 with:
if side == "yes":
    queue_pos = sum(q for p, q in yes_bids if p == best_bid)
else:
    queue_pos = sum(q for p, q in no_bids if p == best_bid)
```

- [ ] **Step 2: Run all tests**

Run: `pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add src/mm/engine.py
git commit -m "fix(mm): use actual book depth for queue position initialization"
```

### Task 7: Smoke test (5-minute run)

- [ ] **Step 1: Run 5-minute test on single market**

```bash
python scripts/paper_mm.py --tickers KXGREENLAND-29 --duration 300
```

Verify:
- API calls succeed (no auth errors)
- DB tables created at `data/mm_paper.db`
- At least 1 tick per market prints to console
- Queue positions are being tracked

- [ ] **Step 2: Check DB was populated**

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/mm_paper.db')
for table in ['mm_orders', 'mm_fills', 'mm_snapshots', 'mm_events']:
    count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f'{table}: {count} rows')
conn.close()
"
```

Expected: `mm_orders` and `mm_snapshots` have rows. `mm_fills` may be 0 if no fills occurred in 5 minutes.

- [ ] **Step 3: Test graceful shutdown (Ctrl+C)**

Run the script again briefly and press Ctrl+C. Verify:
- "Shutting down gracefully..." prints
- Session summary prints
- No tracebacks

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "fix(mm): smoke test fixes"
```

### Task 8: Launch 48-hour paper trading run

- [ ] **Step 1: Start the full run**

```bash
nohup python scripts/paper_mm.py --duration 172800 > data/mm_paper_run.log 2>&1 &
echo $! > data/mm_paper.pid
echo "Paper MM started, PID=$(cat data/mm_paper.pid)"
```

- [ ] **Step 2: Verify it's running**

```bash
tail -20 data/mm_paper_run.log
```

Should show tick output for all 5 markets.

- [ ] **Step 3: Set up Discord completion notification**

The script already sends Discord notifications via `discord_notify()` on start and end. Verify the "Paper MM Started" message arrived in Discord.
