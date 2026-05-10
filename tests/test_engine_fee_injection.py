# tests/test_engine_fee_injection.py
"""Tests for fee-calculator dependency injection in MMEngine.

Polymarket sports makers earn a REBATE (negative fee), while Kalshi makers
PAY a positive fee. Both bots share src/mm/engine.py, so the engine must
not hard-code one platform's formula. The fix injects the platform-correct
fee functions into the engine constructor; defaults preserve Kalshi behavior.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.mm.state import (
    MarketState, GlobalState, SimOrder,
    maker_fee_cents, taker_fee_cents,
)
from src.mm.engine import MMEngine
from src.poly_client import calculate_maker_fee


# ---------------------------------------------------------------------------
# Regression: Kalshi formula unchanged
# ---------------------------------------------------------------------------

def test_kalshi_maker_fee_at_50c_unchanged():
    """Kalshi maker fee at midpoint is positive 0.4375c per contract."""
    assert abs(maker_fee_cents(50, 1) - 0.4375) < 1e-6


def test_kalshi_maker_fee_at_20c_unchanged():
    """P=0.20: 0.0175 * 0.20 * 0.80 * 100 = 0.28c per contract."""
    assert abs(maker_fee_cents(20, 1) - 0.28) < 1e-6


def test_kalshi_maker_fee_at_80c_unchanged():
    """P=0.80: 0.0175 * 0.80 * 0.20 * 100 = 0.28c per contract."""
    assert abs(maker_fee_cents(80, 1) - 0.28) < 1e-6


def test_kalshi_taker_fee_at_50c_unchanged():
    """Kalshi taker fee at midpoint is 1.75c per contract."""
    assert abs(taker_fee_cents(50, 1) - 1.75) < 1e-6


# ---------------------------------------------------------------------------
# Polymarket fee formula (rebate)
# ---------------------------------------------------------------------------

def test_polymarket_sports_maker_at_50c_is_negative():
    """Polymarket sports maker rebate at midpoint = -0.125c per contract."""
    fee = calculate_maker_fee(50, category="sports", count=1)
    assert fee < 0
    assert abs(fee - (-0.125)) < 1e-6


def test_polymarket_sports_maker_at_20c_is_negative():
    """At P=0.20: taker = 0.02 * 0.16 * 100 = 0.32c, rebate = 25% * 0.32 = 0.08c."""
    fee = calculate_maker_fee(20, category="sports", count=1)
    assert fee < 0
    assert abs(fee - (-0.08)) < 1e-6


def test_polymarket_sports_maker_at_80c_is_negative():
    """At P=0.80: same magnitude as 20c (P*(1-P) symmetric)."""
    fee = calculate_maker_fee(80, category="sports", count=1)
    assert fee < 0
    assert abs(fee - (-0.08)) < 1e-6


def test_polymarket_sports_maker_at_0c_is_zero():
    """Degenerate price: no rebate (P*(1-P)=0)."""
    assert calculate_maker_fee(0, category="sports", count=1) == 0


def test_polymarket_sports_maker_at_100c_is_zero():
    """Degenerate price: no rebate (P*(1-P)=0)."""
    assert calculate_maker_fee(100, category="sports", count=1) == 0


# ---------------------------------------------------------------------------
# Engine wiring: injected calculator must be called
# ---------------------------------------------------------------------------

def _make_engine_with_calculators(maker_calc=None, taker_calc=None):
    gs = GlobalState(session_id="test")
    gs.markets["MKT"] = MarketState(ticker="MKT")
    client = MagicMock()
    db = MagicMock()
    db.insert_order.return_value = 1
    kwargs = {"order_size": 2}
    if maker_calc is not None:
        kwargs["maker_fee_calculator"] = maker_calc
    if taker_calc is not None:
        kwargs["taker_fee_calculator"] = taker_calc
    engine = MMEngine(client, db, gs, **kwargs)
    return engine, gs


def test_engine_default_uses_kalshi_maker_formula():
    """Without injection, MMEngine preserves Kalshi maker behavior."""
    engine, gs = _make_engine_with_calculators()
    ms = gs.markets["MKT"]
    order = SimOrder(side="yes", price=50, size=1, remaining=1,
                     queue_pos=0, placed_at=datetime.now(timezone.utc))
    order.db_id = 1
    engine._record_fill(ms, order, 1, 50, 50)
    # Kalshi fee at 50c, count=1 → +0.4375c
    args = engine.db.insert_fill.call_args
    assert args is not None
    assert abs(args.kwargs["fee"] - 0.4375) < 1e-6


def test_engine_uses_injected_maker_calculator():
    """Engine calls injected maker_fee_calculator(price, size) for fills."""
    sentinel = -7.42
    calls = []

    def stub(price, size):
        calls.append((price, size))
        return sentinel

    engine, gs = _make_engine_with_calculators(maker_calc=stub)
    ms = gs.markets["MKT"]
    order = SimOrder(side="yes", price=50, size=2, remaining=2,
                     queue_pos=0, placed_at=datetime.now(timezone.utc))
    order.db_id = 1
    engine._record_fill(ms, order, 2, 50, 50)

    assert calls == [(50, 2)]
    args = engine.db.insert_fill.call_args
    assert args is not None
    assert args.kwargs["fee"] == sentinel


def test_engine_uses_injected_taker_calculator_for_aggress():
    """Engine calls injected taker_fee_calculator on aggress flatten."""
    sentinel = 2.71
    calls = []

    def stub(price, size):
        calls.append((price, size))
        return sentinel

    engine, gs = _make_engine_with_calculators(taker_calc=stub)
    ms = gs.markets["MKT"]
    # Long YES → engine will buy NO to flatten
    ms.yes_queue = [45, 45]
    engine._aggress_flatten(ms, best_yes_bid=45, yes_ask=48,
                            best_no_bid=52, midpoint=46.5)

    # taker calculator was called with (price, size)
    assert len(calls) == 1
    assert calls[0][1] == ms._initial_yes_inv if False else calls[0][1] == 2  # size = abs(net) clamped to order_size=2
    args = engine.db.insert_fill.call_args
    assert args is not None
    assert args.kwargs["fee"] == sentinel
    assert args.kwargs["is_taker"] == 1


def test_engine_polymarket_maker_records_negative_fee():
    """End-to-end: passing the Polymarket calculator records negative fee."""

    def poly_maker(price, size):
        return calculate_maker_fee(price, category="sports", count=size)

    engine, gs = _make_engine_with_calculators(maker_calc=poly_maker)
    ms = gs.markets["MKT"]
    order = SimOrder(side="yes", price=50, size=2, remaining=2,
                     queue_pos=0, placed_at=datetime.now(timezone.utc))
    order.db_id = 1
    engine._record_fill(ms, order, 2, 50, 50)

    args = engine.db.insert_fill.call_args
    assert args is not None
    fee = args.kwargs["fee"]
    # 2 contracts at 50c: rebate = -0.25c (negative = earned)
    assert fee < 0
    assert abs(fee - (-0.25)) < 1e-6
    # P&L should INCREASE (rebate boosts P&L, not reduce it)
    # _record_fill does: realized_pnl -= fee; with fee=-0.25, pnl += 0.25
    assert ms.realized_pnl > 0
    assert abs(ms.realized_pnl - 0.25) < 1e-6


def test_engine_polymarket_at_degenerate_price_zero_fee():
    """At P=0 or P=100, Polymarket rebate is 0 (no fee)."""

    def poly_maker(price, size):
        return calculate_maker_fee(price, category="sports", count=size)

    engine, gs = _make_engine_with_calculators(maker_calc=poly_maker)
    ms = gs.markets["MKT"]
    order = SimOrder(side="yes", price=100, size=1, remaining=1,
                     queue_pos=0, placed_at=datetime.now(timezone.utc))
    order.db_id = 1
    engine._record_fill(ms, order, 1, 100, 0)

    args = engine.db.insert_fill.call_args
    assert args is not None
    assert args.kwargs["fee"] == 0
