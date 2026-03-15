# tests/test_auto_deactivate.py
"""Tests for auto-deactivation of dead orderbook markets.

After 30 consecutive empty orderbook ticks (~5 min at 10s interval),
a market should be deactivated with reason 'orderbook_dead'.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from src.mm.state import MarketState, GlobalState
from src.mm.engine import MMEngine
from src.mm.risk import Action


def _make_engine(tickers: list[str]) -> tuple[MMEngine, GlobalState]:
    gs = GlobalState(session_id="test")
    for t in tickers:
        gs.markets[t] = MarketState(ticker=t)
    client = MagicMock()
    db = MagicMock()
    db.insert_order.return_value = 1
    engine = MMEngine(client, db, gs, order_size=2)
    return engine, gs


def _empty_book():
    return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}


def _standard_book():
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }


def _no_trades():
    return {"trades": []}


class TestAutoDeactivateDeadOrderbook:
    def test_consecutive_skip_ticks_increment(self):
        """Empty book increments consecutive_skip_ticks."""
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        engine.client.get_orderbook.return_value = _empty_book()
        engine.client.get_trades.return_value = _no_trades()

        engine.tick_one_market(ms)
        assert ms.consecutive_skip_ticks == 1
        assert ms.active is True

        engine.tick_one_market(ms)
        assert ms.consecutive_skip_ticks == 2
        assert ms.active is True

    def test_deactivate_after_30_empty_ticks(self):
        """Market deactivates after 30 consecutive empty orderbook ticks."""
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        engine.client.get_orderbook.return_value = _empty_book()
        engine.client.get_trades.return_value = _no_trades()

        # Run 29 ticks — should remain active
        for _ in range(29):
            engine.tick_one_market(ms)
        assert ms.active is True
        assert ms.consecutive_skip_ticks == 29

        # 30th tick — should deactivate
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine.tick_one_market(ms)
            assert ms.active is False
            assert ms.deactivation_reason == "orderbook_dead"
            # Discord fires for both the deactivation alert and EXIT_MARKET log
            assert mock_discord.call_count == 2
            msgs = [c[0][0] for c in mock_discord.call_args_list]
            assert any("orderbook dead" in m for m in msgs)

    def test_counter_resets_on_good_tick(self):
        """Good orderbook data resets the consecutive skip counter."""
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        engine.client.get_trades.return_value = _no_trades()

        # 10 empty ticks
        engine.client.get_orderbook.return_value = _empty_book()
        for _ in range(10):
            engine.tick_one_market(ms)
        assert ms.consecutive_skip_ticks == 10

        # One good tick resets counter
        engine.client.get_orderbook.return_value = _standard_book()
        with patch("src.mm.engine.check_layer4", return_value=Action.CONTINUE):
            engine.tick_one_market(ms)
        assert ms.consecutive_skip_ticks == 0
        assert ms.active is True

    def test_deactivation_cancels_orders(self):
        """Deactivation should cancel any resting orders."""
        from src.mm.state import SimOrder
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        now = datetime.now(timezone.utc)
        ms.yes_order = SimOrder(side="yes", price=45, size=2,
                                remaining=2, queue_pos=100, placed_at=now)
        ms.no_order = SimOrder(side="no", price=53, size=2,
                               remaining=2, queue_pos=100, placed_at=now)

        engine.client.get_orderbook.return_value = _empty_book()
        engine.client.get_trades.return_value = _no_trades()

        # Run 30 ticks to deactivate
        ms.consecutive_skip_ticks = 29
        with patch("src.mm.engine.discord_notify"):
            engine.tick_one_market(ms)

        assert ms.active is False
        assert ms.yes_order is None
        assert ms.no_order is None

    def test_only_first_skip_logged(self):
        """Only the first empty tick logs a SKIP_TICK event, not every tick."""
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        engine.client.get_orderbook.return_value = _empty_book()
        engine.client.get_trades.return_value = _no_trades()

        # First tick: should call insert_event (SKIP_TICK)
        engine.tick_one_market(ms)
        first_call_count = engine.db.insert_event.call_count

        # Second tick: should NOT call insert_event again
        engine.tick_one_market(ms)
        assert engine.db.insert_event.call_count == first_call_count
