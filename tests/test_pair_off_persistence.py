# tests/test_pair_off_persistence.py
"""Pair-off attribution to mm_fills DB rows.

Verifies that when pair_off_inventory runs, the involved fills get
non-NULL pair_id and pair_pnl in the database. Production data (May
2026) showed all 326 historical fills had NULL pair_id/pair_pnl —
this regression test prevents that recurring.

Design:
  - MarketState tracks parallel fill_id queues alongside yes/no_queue.
  - pair_off_inventory pops both queues; returns yes_fill_id/no_fill_id
    in each pair dict.
  - Engine bumps gs.pair_seq once per non-empty pair-off cycle and
    calls db.update_fill(fid, pair_id=..., pair_pnl=...) for each
    distinct fill_id participating in the cycle. pair_pnl is the
    SUM of gross_pnl across all pairs in that cycle that touched
    this fill (relevant when a single fill participates in multiple
    pairs from the same pair-off call).
"""
from datetime import datetime, timezone

import pytest

from src.mm.db import MMDatabase
from src.mm.engine import MMEngine, pair_off_inventory
from src.mm.state import MarketState, GlobalState, SimOrder


# -- Helpers ----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_db(tmp_path) -> MMDatabase:
    return MMDatabase(str(tmp_path / "t.db"), session_id="s1")


def _insert_fill(db: MMDatabase, side: str, price: int, size: int = 1,
                 inv_after: int = 0) -> int:
    return db.insert_fill(
        order_id=None, ticker="X", side=f"{side}_bid",
        price=price, size=size, fee=0.0, is_taker=0,
        inventory_after=inv_after, filled_at=_now_iso())


def _fetch_fill(db: MMDatabase, fill_id: int) -> dict:
    cur = db.conn.execute(
        "SELECT pair_id, pair_pnl FROM mm_fills WHERE id=?", (fill_id,))
    row = cur.fetchone()
    return {"pair_id": row[0], "pair_pnl": row[1]}


def _make_engine(db: MMDatabase, gs: GlobalState) -> MMEngine:
    # MMEngine doesn't dispatch DB only for pair-off, so a client stub is fine.
    return MMEngine(client=None, db=db, global_state=gs, order_size=2)


# -- 1. pair_off_inventory function signature ------------------------------

def test_pair_off_returns_fill_ids_when_present():
    """pair_off_inventory should return yes_fill_id/no_fill_id when
    MarketState carries parallel fill-id queues."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [45]
    ms.no_queue = [55]
    ms.yes_fill_ids = [101]
    ms.no_fill_ids = [202]

    pairs = pair_off_inventory(ms)
    assert len(pairs) == 1
    assert pairs[0]["yes_fill_id"] == 101
    assert pairs[0]["no_fill_id"] == 202
    assert pairs[0]["gross_pnl"] == 0  # 100 - 45 - 55


def test_pair_off_returns_none_fill_ids_when_absent():
    """Backward-compat: tests that mutate yes_queue/no_queue directly
    (without populating yes_fill_ids) still work; fill_ids are None."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [45]
    ms.no_queue = [55]
    # yes_fill_ids/no_fill_ids default to empty list

    pairs = pair_off_inventory(ms)
    assert len(pairs) == 1
    assert pairs[0]["yes_fill_id"] is None
    assert pairs[0]["no_fill_id"] is None


def test_pair_off_multi_pair_pops_fill_ids_in_order():
    """Two pairs in one cycle: first pair gets first fill_ids, etc."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [45, 45]
    ms.no_queue = [55, 55]
    ms.yes_fill_ids = [101, 101]  # same fill, size=2
    ms.no_fill_ids = [202, 202]

    pairs = pair_off_inventory(ms)
    assert len(pairs) == 2
    for p in pairs:
        assert p["yes_fill_id"] == 101
        assert p["no_fill_id"] == 202


# -- 2. MarketState carries fill-id queues ---------------------------------

def test_market_state_has_fill_id_queue_fields():
    """MarketState should expose yes_fill_ids and no_fill_ids."""
    ms = MarketState(ticker="X")
    assert ms.yes_fill_ids == []
    assert ms.no_fill_ids == []


# -- 3. db.update_fill writes pair_id and pair_pnl -------------------------

def test_db_update_fill_sets_pair_id_and_pnl(tmp_path):
    db = _make_db(tmp_path)
    fid = _insert_fill(db, "yes", 45)
    # before: NULL
    assert _fetch_fill(db, fid) == {"pair_id": None, "pair_pnl": None}

    db.update_fill(fid, pair_id=1, pair_pnl=2.5)

    assert _fetch_fill(db, fid) == {"pair_id": 1, "pair_pnl": 2.5}


def test_db_update_fill_partial_update(tmp_path):
    """Only fields passed are updated; others remain."""
    db = _make_db(tmp_path)
    fid = _insert_fill(db, "yes", 45)
    db.update_fill(fid, pair_id=7, pair_pnl=1.0)
    db.update_fill(fid, pair_pnl=5.0)  # pair_id should remain 7

    assert _fetch_fill(db, fid) == {"pair_id": 7, "pair_pnl": 5.0}


# -- 4. GlobalState carries pair_seq counter -------------------------------

def test_global_state_has_pair_seq_counter():
    gs = GlobalState()
    assert gs.pair_seq == 0


# -- 5. Engine writes pair_id/pair_pnl to DB after pair-off ----------------

def test_engine_pair_off_writes_to_db_simple(tmp_path):
    """Single pair (yes@45 + no@55) → both fills get pair_id=1, pair_pnl=0."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    yes_fid = _insert_fill(db, "yes", 45)
    no_fid = _insert_fill(db, "no", 55)
    ms.yes_queue = [45]
    ms.no_queue = [55]
    ms.yes_fill_ids = [yes_fid]
    ms.no_fill_ids = [no_fid]

    engine._process_pair_off(ms)

    assert gs.pair_seq == 1
    assert _fetch_fill(db, yes_fid) == {"pair_id": 1, "pair_pnl": 0.0}
    assert _fetch_fill(db, no_fid) == {"pair_id": 1, "pair_pnl": 0.0}
    assert ms.realized_pnl == 0.0  # 100 - 45 - 55


def test_engine_pair_off_profitable_pair(tmp_path):
    """Profitable pair: yes@40 + no@50 → gross=10, both fills get pair_pnl=10."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    yes_fid = _insert_fill(db, "yes", 40)
    no_fid = _insert_fill(db, "no", 50)
    ms.yes_queue = [40]
    ms.no_queue = [50]
    ms.yes_fill_ids = [yes_fid]
    ms.no_fill_ids = [no_fid]

    engine._process_pair_off(ms)

    assert _fetch_fill(db, yes_fid) == {"pair_id": 1, "pair_pnl": 10.0}
    assert _fetch_fill(db, no_fid) == {"pair_id": 1, "pair_pnl": 10.0}
    assert ms.realized_pnl == 10.0


def test_engine_partial_offset_only_pairs_overlap(tmp_path):
    """yes size=2 + no size=1 → one pair created; yes fill gets pair_id,
    second yes contract stays in queue unpaired."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    yes_fid = _insert_fill(db, "yes", 45, size=2)
    no_fid = _insert_fill(db, "no", 55, size=1)
    ms.yes_queue = [45, 45]
    ms.no_queue = [55]
    ms.yes_fill_ids = [yes_fid, yes_fid]  # same fill, two contracts
    ms.no_fill_ids = [no_fid]

    engine._process_pair_off(ms)

    # 1 pair produced
    assert gs.pair_seq == 1
    # yes still has 1 contract unpaired
    assert ms.yes_queue == [45]
    assert ms.yes_fill_ids == [yes_fid]
    assert ms.no_queue == []
    assert ms.no_fill_ids == []
    # Both fill rows marked
    assert _fetch_fill(db, yes_fid) == {"pair_id": 1, "pair_pnl": 0.0}
    assert _fetch_fill(db, no_fid) == {"pair_id": 1, "pair_pnl": 0.0}


def test_engine_multi_pair_same_cycle_aggregates_pair_pnl(tmp_path):
    """yes size=2@40 + no size=2@45 → 2 pairs in one cycle.
    Both fills get same pair_id; pair_pnl = sum of gross across both pairs."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    yes_fid = _insert_fill(db, "yes", 40, size=2)
    no_fid = _insert_fill(db, "no", 45, size=2)
    ms.yes_queue = [40, 40]
    ms.no_queue = [45, 45]
    ms.yes_fill_ids = [yes_fid, yes_fid]
    ms.no_fill_ids = [no_fid, no_fid]

    engine._process_pair_off(ms)

    # Two pairs, each gross = 100-40-45 = 15; aggregated per fill = 30
    assert gs.pair_seq == 1  # single cycle
    assert _fetch_fill(db, yes_fid) == {"pair_id": 1, "pair_pnl": 30.0}
    assert _fetch_fill(db, no_fid) == {"pair_id": 1, "pair_pnl": 30.0}
    assert ms.realized_pnl == 30.0


def test_engine_sequential_cycles_get_distinct_pair_ids(tmp_path):
    """Two separate pair-off calls produce pair_id=1 and pair_id=2."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    # First cycle
    y1 = _insert_fill(db, "yes", 45)
    n1 = _insert_fill(db, "no", 55)
    ms.yes_queue = [45]; ms.no_queue = [55]
    ms.yes_fill_ids = [y1]; ms.no_fill_ids = [n1]
    engine._process_pair_off(ms)

    # Second cycle
    y2 = _insert_fill(db, "yes", 40)
    n2 = _insert_fill(db, "no", 50)
    ms.yes_queue = [40]; ms.no_queue = [50]
    ms.yes_fill_ids = [y2]; ms.no_fill_ids = [n2]
    engine._process_pair_off(ms)

    assert gs.pair_seq == 2
    assert _fetch_fill(db, y1)["pair_id"] == 1
    assert _fetch_fill(db, y2)["pair_id"] == 2


def test_engine_unpaired_fill_keeps_null_pair_id(tmp_path):
    """A fill sitting in queue with no offset has NULL pair_id."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    yes_fid = _insert_fill(db, "yes", 45)
    ms.yes_queue = [45]
    ms.yes_fill_ids = [yes_fid]
    # no_queue empty — no pair-off can happen

    engine._process_pair_off(ms)

    assert gs.pair_seq == 0  # no cycle ran
    assert _fetch_fill(db, yes_fid) == {"pair_id": None, "pair_pnl": None}


def test_engine_pair_off_increments_consecutive_losses_on_negative(tmp_path):
    """Preserve existing behavior: negative gross_pnl increments consecutive_losses."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    yes_fid = _insert_fill(db, "yes", 60)
    no_fid = _insert_fill(db, "no", 50)
    ms.yes_queue = [60]
    ms.no_queue = [50]
    ms.yes_fill_ids = [yes_fid]
    ms.no_fill_ids = [no_fid]
    # 100 - 60 - 50 = -10 (loss)

    engine._process_pair_off(ms)

    assert ms.consecutive_losses == 1
    assert ms.realized_pnl == -10.0
    assert _fetch_fill(db, yes_fid) == {"pair_id": 1, "pair_pnl": -10.0}


def test_engine_pair_off_resets_consecutive_losses_on_win(tmp_path):
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    ms.consecutive_losses = 2
    yes_fid = _insert_fill(db, "yes", 40)
    no_fid = _insert_fill(db, "no", 50)
    ms.yes_queue = [40]; ms.no_queue = [50]
    ms.yes_fill_ids = [yes_fid]; ms.no_fill_ids = [no_fid]
    # gross = 10 (win)

    engine._process_pair_off(ms)
    assert ms.consecutive_losses == 0


def test_engine_pair_off_resets_oldest_fill_time_when_flat(tmp_path):
    """If queues drained fully, oldest_fill_time reset to None."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    ms.oldest_fill_time = datetime.now(timezone.utc)
    ms.skew_activated_at = datetime.now(timezone.utc)
    yes_fid = _insert_fill(db, "yes", 45)
    no_fid = _insert_fill(db, "no", 55)
    ms.yes_queue = [45]; ms.no_queue = [55]
    ms.yes_fill_ids = [yes_fid]; ms.no_fill_ids = [no_fid]

    engine._process_pair_off(ms)

    assert ms.oldest_fill_time is None
    assert ms.skew_activated_at is None


def test_engine_pair_off_no_pairs_does_not_bump_seq(tmp_path):
    """Empty queues → no DB writes, pair_seq unchanged."""
    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    engine = _make_engine(db, gs)

    ms = MarketState(ticker="X")
    engine._process_pair_off(ms)

    assert gs.pair_seq == 0


# -- 6. Free function process_pair_off (shared by paper + live) -----------

def test_process_pair_off_free_function(tmp_path):
    """The free function should work standalone (used by poly_live_mm.py).
    Simulates the live-fill flow: insert fill row, extend queue+fill_id
    queue in lockstep, opposing fill, then pair off."""
    from src.mm.engine import process_pair_off

    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    ms = MarketState(ticker="X")

    # Simulate first fill: yes_bid @ 45c size=1
    yes_fid = _insert_fill(db, "yes", 45, size=1, inv_after=1)
    ms.yes_queue.extend([45])
    ms.yes_fill_ids.extend([yes_fid])

    # Simulate offsetting fill: no_bid @ 55c size=1
    no_fid = _insert_fill(db, "no", 55, size=1, inv_after=0)
    ms.no_queue.extend([55])
    ms.no_fill_ids.extend([no_fid])

    n_pairs = process_pair_off(ms, gs, db)

    assert n_pairs == 1
    assert gs.pair_seq == 1
    assert ms.realized_pnl == 0.0
    assert ms.paired_fills == 1
    assert _fetch_fill(db, yes_fid) == {"pair_id": 1, "pair_pnl": 0.0}
    assert _fetch_fill(db, no_fid) == {"pair_id": 1, "pair_pnl": 0.0}


def test_process_pair_off_returns_zero_when_no_pairs(tmp_path):
    from src.mm.engine import process_pair_off

    db = _make_db(tmp_path)
    gs = GlobalState(session_id="s1")
    ms = MarketState(ticker="X")
    ms.yes_queue = [45]
    ms.yes_fill_ids = [_insert_fill(db, "yes", 45)]

    n_pairs = process_pair_off(ms, gs, db)
    assert n_pairs == 0
    assert gs.pair_seq == 0
