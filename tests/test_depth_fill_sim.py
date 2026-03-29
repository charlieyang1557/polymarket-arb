# tests/test_depth_fill_sim.py
"""Tests for orderbook-snapshot fill simulation."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_paper_mm import (
    compute_depth_at_price,
    compute_drain,
    DepthFillSimulator,
    MAX_DRAIN_PER_TICK,
)
from src.mm.state import SimOrder
from src.poly_client import calculate_maker_fee
from datetime import datetime, timezone


def _order(side="yes", price=55, size=2, queue_pos=100):
    return SimOrder(
        side=side, price=price, size=size, remaining=size,
        queue_pos=queue_pos,
        placed_at=datetime.now(timezone.utc),
    )


# --- compute_depth_at_price ---

def test_depth_at_price_yes():
    """YES depth at price P = sum of qty at levels where price <= P."""
    book = [[50, 200], [53, 150], [55, 100], [57, 80]]  # sorted asc
    assert compute_depth_at_price(book, 55, side="yes") == 450  # 200+150+100


def test_depth_at_price_yes_exact():
    """At our price exactly, include that level."""
    book = [[55, 100]]
    assert compute_depth_at_price(book, 55, side="yes") == 100


def test_depth_at_price_yes_none():
    """No levels at or below price → 0."""
    book = [[57, 100]]
    assert compute_depth_at_price(book, 55, side="yes") == 0


def test_depth_at_price_no():
    """NO depth at price P = sum of qty at levels where price <= P."""
    book = [[40, 200], [42, 150], [45, 100], [48, 80]]  # sorted asc
    assert compute_depth_at_price(book, 45, side="no") == 450


def test_depth_at_price_empty():
    assert compute_depth_at_price([], 55, side="yes") == 0


# --- compute_drain ---

def test_drain_depth_decrease():
    """Depth shrank by 200 → drain = 200 * 0.5 = 100."""
    drain = compute_drain(prev_depth=500, curr_depth=300, factor=0.5)
    assert drain == 100


def test_drain_depth_increase():
    """Depth grew → no drain (new orders added, not consumed)."""
    drain = compute_drain(prev_depth=300, curr_depth=500, factor=0.5)
    assert drain == 0


def test_drain_no_change():
    drain = compute_drain(prev_depth=500, curr_depth=500, factor=0.5)
    assert drain == 0


def test_drain_level_wipeout():
    """Depth went to 0 → all depth was consumed."""
    drain = compute_drain(prev_depth=500, curr_depth=0, factor=0.5)
    assert drain == 250


def test_drain_factor_1():
    """Factor 1.0 = aggressive (all depth decrease = real trades)."""
    drain = compute_drain(prev_depth=500, curr_depth=300, factor=1.0)
    assert drain == 200


# --- DepthFillSimulator ---

def test_sim_no_orders():
    """No resting orders → no fills, no crash."""
    sim = DepthFillSimulator()
    fills = sim.check_fills("slug", None, None, [], [])
    assert fills == []


def test_sim_first_tick():
    """First tick sets baseline depth, no fills."""
    sim = DepthFillSimulator()
    order = _order(side="yes", price=55, queue_pos=100)
    book = [[50, 200], [55, 100]]
    fills = sim.check_fills("slug", order, None, book, [])
    assert fills == []
    # Second tick with same depth — no fills
    fills2 = sim.check_fills("slug", order, None, book, [])
    assert fills2 == []


def test_sim_drain_advances_queue():
    """Depth decrease drains queue position."""
    sim = DepthFillSimulator()
    order = _order(side="yes", price=55, queue_pos=200)
    book = [[50, 200], [55, 100]]

    # Tick 1: set baseline (depth_at_55 = 300)
    sim.check_fills("slug", order, None, book, [])

    # Tick 2: depth decreased to 200 (delta=100, drain=50)
    book2 = [[50, 100], [55, 100]]
    fills = sim.check_fills("slug", order, None, book2, [])
    assert fills == []
    assert order.queue_pos == 150  # 200 - 50


def test_sim_fill_triggered():
    """Queue drained past 0 → fill triggered."""
    sim = DepthFillSimulator()
    order = _order(side="yes", price=55, size=2, queue_pos=20)
    book = [[50, 200], [55, 100]]

    # Tick 1: baseline
    sim.check_fills("slug", order, None, book, [])

    # Tick 2: massive depth decrease (drain 150*0.5=75 > queue 20)
    book2 = [[50, 50], [55, 50]]
    fills = sim.check_fills("slug", order, None, book2, [])
    assert len(fills) == 1
    assert fills[0]["side"] == "yes"
    assert fills[0]["filled"] == 2
    assert fills[0]["price"] == 55


def test_sim_partial_fill():
    """Drain exceeds queue but less than queue+size → partial fill possible."""
    sim = DepthFillSimulator()
    order = _order(side="yes", price=55, size=5, queue_pos=10)
    book = [[55, 200]]

    sim.check_fills("slug", order, None, book, [])

    # Drain = (200-150)*0.5 = 25. queue=10, overflow=15, fill=min(5,15)=5
    book2 = [[55, 150]]
    fills = sim.check_fills("slug", order, None, book2, [])
    assert len(fills) == 1
    assert fills[0]["filled"] == 5


def test_sim_level_wipeout_instant_fill():
    """Our price level completely disappeared → instant fill."""
    sim = DepthFillSimulator()
    order = _order(side="yes", price=55, size=2, queue_pos=50)
    book = [[50, 100], [55, 200]]

    sim.check_fills("slug", order, None, book, [])

    # Level at 55 gone entirely — all depth consumed
    book2 = [[50, 100]]
    fills = sim.check_fills("slug", order, None, book2, [])
    assert len(fills) == 1
    assert fills[0]["filled"] == 2


def test_sim_no_side():
    """NO side fill works symmetrically."""
    sim = DepthFillSimulator()
    order = _order(side="no", price=45, size=2, queue_pos=20)
    book = [[40, 100], [45, 200]]

    sim.check_fills("slug", None, order, [], book)

    # Depth decrease on NO side
    book2 = [[40, 50], [45, 100]]
    fills = sim.check_fills("slug", None, order, [], book2)
    assert len(fills) == 1
    assert fills[0]["side"] == "no"


def test_sim_both_sides():
    """Both sides can fill in same tick."""
    sim = DepthFillSimulator()
    yes_order = _order(side="yes", price=55, size=2, queue_pos=10)
    no_order = _order(side="no", price=45, size=2, queue_pos=10)
    yes_book = [[55, 100]]
    no_book = [[45, 100]]

    sim.check_fills("slug", yes_order, no_order, yes_book, no_book)

    # Both sides depth decrease
    yes_book2 = [[55, 50]]
    no_book2 = [[45, 50]]
    fills = sim.check_fills("slug", yes_order, no_order, yes_book2, no_book2)
    assert len(fills) == 2


def test_sim_order_replaced():
    """New order at different price resets baseline."""
    sim = DepthFillSimulator()
    order1 = _order(side="yes", price=55, queue_pos=100)
    book = [[55, 200]]
    sim.check_fills("slug", order1, None, book, [])

    # Order replaced at different price
    order2 = _order(side="yes", price=53, queue_pos=150)
    book2 = [[53, 300]]
    fills = sim.check_fills("slug", order2, None, book2, [])
    assert fills == []  # baseline reset, no fills on first tick at new price


# --- Fee monkey-patch verification ---

def test_maker_fee_is_negative_after_monkeypatch():
    """_apply_poly_fee_patch makes maker_fee_cents return negative (rebate)."""
    import src.mm.state as mm_state
    from scripts.poly_paper_mm import _apply_poly_fee_patch

    # Save original, apply patch, test, restore
    original = mm_state.maker_fee_cents
    try:
        _apply_poly_fee_patch()
        fee = mm_state.maker_fee_cents(50, 2)
        # Polymarket sports: rebate = -0.25 * 0.02 * 0.5 * 0.5 * 100 * 2 = -0.25c
        assert fee < 0, f"Expected negative fee (rebate), got {fee}"
        assert abs(fee - (-0.25)) < 0.01
    finally:
        mm_state.maker_fee_cents = original


def test_fill_at_50c_shows_rebate():
    """A fill at 50c should show negative fee = maker gets paid."""
    fee = calculate_maker_fee(50, category="sports", count=2)
    assert fee < 0
    # At 50c: rebate = 0.25 * 0.02 * 0.25 * 100 * 2 = 0.25c → returned as -0.25
    assert abs(fee - (-0.25)) < 0.01


# --- Drain sanity cap ---

def test_drain_capped_at_max():
    """Drain > MAX_DRAIN_PER_TICK gets capped."""
    drain = compute_drain(prev_depth=200000, curr_depth=0, factor=0.5)
    # Raw: 200000 * 0.5 = 100000, but capped to MAX_DRAIN_PER_TICK
    assert drain == MAX_DRAIN_PER_TICK


def test_drain_under_max_not_capped():
    """Drain under threshold passes through unchanged."""
    drain = compute_drain(prev_depth=1000, curr_depth=0, factor=0.5)
    assert drain == 500  # 1000 * 0.5, below cap


def test_sim_caps_extreme_drain(capsys):
    """Simulator logs DRAIN_CAP and caps the drain."""
    sim = DepthFillSimulator()
    order = _order(side="yes", price=55, size=2, queue_pos=10)
    book = [[55, 300000]]

    sim.check_fills("slug", order, None, book, [])

    # Massive depth drop
    book2 = [[55, 10000]]
    fills = sim.check_fills("slug", order, None, book2, [])
    # Fill triggered (queue_pos=10 easily drained)
    assert len(fills) == 1
    # Drain in fill dict should be capped
    assert fills[0]["drain"] <= MAX_DRAIN_PER_TICK
