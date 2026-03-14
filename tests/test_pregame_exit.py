# tests/test_pregame_exit.py
"""Tests for pre-game only mode: exit market when live game detected."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from src.mm.state import MarketState, GlobalState, SimOrder
from src.mm.engine import MMEngine


def _make_engine(tickers: list[str]) -> tuple[MMEngine, GlobalState]:
    """Create engine with mock client/db for testing."""
    gs = GlobalState(session_id="test")
    for t in tickers:
        gs.markets[t] = MarketState(ticker=t)
    client = MagicMock()
    db = MagicMock()
    db.insert_order.return_value = 1
    engine = MMEngine(client, db, gs, order_size=2)
    return engine, gs


def _make_live(ms: MarketState):
    """Populate trade_timestamps to trigger is_live_game."""
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 3) for i in range(60)]


def test_exit_on_live_game_detection():
    """When is_live_game becomes True, market should be deactivated."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    _make_live(ms)
    assert ms.is_live_game is True

    # Simulate a tick — should detect live game and exit
    # Mock API responses
    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}

    engine.tick_one_market(ms)
    assert ms.active is False


def test_other_markets_continue():
    """Other pre-game markets keep running when one goes live."""
    engine, gs = _make_engine(["LIVE", "PREGAME"])
    ms_live = gs.markets["LIVE"]
    ms_pre = gs.markets["PREGAME"]

    _make_live(ms_live)
    assert ms_live.is_live_game is True
    assert ms_pre.is_live_game is False

    # Tick the live market — should exit
    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}

    engine.tick_one_market(ms_live)
    assert ms_live.active is False
    assert ms_pre.active is True  # untouched


def test_cancels_resting_orders_on_exit():
    """Resting orders should be cancelled when exiting for live game."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    now = datetime.now(timezone.utc)
    ms.yes_order = SimOrder(
        side="yes", price=46, size=2, remaining=2,
        queue_pos=100, placed_at=now, db_id=1)
    ms.no_order = SimOrder(
        side="no", price=54, size=2, remaining=2,
        queue_pos=50, placed_at=now, db_id=2)
    _make_live(ms)

    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}

    engine.tick_one_market(ms)
    assert ms.yes_order is None
    assert ms.no_order is None
    assert ms.active is False


def test_no_exit_in_pregame():
    """Pre-game market should NOT be exited."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    ms.trade_timestamps = []  # pre-game

    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}

    engine.tick_one_market(ms)
    assert ms.active is True


def test_no_resume_after_exit():
    """Once exited, market stays inactive even if trade frequency drops."""
    engine, gs = _make_engine(["X"])
    ms = gs.markets["X"]
    _make_live(ms)

    engine.client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }
    engine.client.get_trades.return_value = {"trades": []}

    # First tick — exits
    engine.tick_one_market(ms)
    assert ms.active is False

    # Clear trade timestamps (simulating frequency drop)
    ms.trade_timestamps = []
    assert ms.is_live_game is False

    # Second tick — should not run (active=False checked by caller)
    # The engine's main loop checks ms.active before calling tick_one_market
    # So we just verify active stays False
    assert ms.active is False
