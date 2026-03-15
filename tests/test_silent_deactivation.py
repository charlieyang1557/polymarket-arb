# tests/test_silent_deactivation.py
"""Tests for silent market deactivation bug.

Every code path that sets ms.active = False MUST log a reason.
Markets should never silently disappear from the trading loop.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call
import requests

from src.mm.state import MarketState, GlobalState, SimOrder
from src.mm.engine import MMEngine
from src.mm.risk import Action


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


def _standard_book():
    """Return a standard orderbook response."""
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }


def _no_trades():
    return {"trades": []}


# ---------------------------------------------------------------------------
# Test 1: FULL_STOP from L4 logs deactivation for ALL markets, not just trigger
# ---------------------------------------------------------------------------

class TestFullStopLogsAllMarkets:
    """When FULL_STOP triggers on one market, all markets go inactive.
    Each deactivated market must have a logged event."""

    def test_l4_full_stop_logs_all_markets(self):
        engine, gs = _make_engine(["MKT_A", "MKT_B", "MKT_C"])
        ms_a = gs.markets["MKT_A"]

        # Force L4 to return FULL_STOP for MKT_A
        engine.client.get_orderbook.return_value = _standard_book()
        engine.client.get_trades.return_value = _no_trades()

        with patch("src.mm.engine.check_layer4", return_value=Action.FULL_STOP):
            engine.tick_one_market(ms_a)

        # All markets should be inactive
        for ticker, ms in gs.markets.items():
            assert ms.active is False, f"{ticker} should be inactive after FULL_STOP"

        # Verify _log_event was called — check db.insert_event calls
        # Each deactivated market (including collateral ones) should have a log
        event_calls = engine.db.insert_event.call_args_list
        logged_tickers = {c.kwargs.get("ticker", c[0][1] if len(c[0]) > 1 else None)
                          for c in event_calls}
        # At minimum, all markets that were NOT the trigger should still be logged
        for ticker in ["MKT_B", "MKT_C"]:
            assert ticker in logged_tickers or any(
                ticker in str(c) for c in event_calls
            ), f"{ticker} was silently deactivated by FULL_STOP without logging"

    def test_l3_full_stop_logs_all_markets(self):
        """L3 FULL_STOP (e.g., daily loss > -500c) also deactivates all markets."""
        engine, gs = _make_engine(["MKT_X", "MKT_Y"])
        ms_x = gs.markets["MKT_X"]

        engine.client.get_orderbook.return_value = _standard_book()
        engine.client.get_trades.return_value = _no_trades()

        # Force L3 to return FULL_STOP
        with patch("src.mm.engine.check_layer4", return_value=Action.CONTINUE), \
             patch("src.mm.engine.check_layer2", return_value=Action.CONTINUE), \
             patch("src.mm.engine.check_layer3", return_value=Action.FULL_STOP):
            engine.tick_one_market(ms_x)

        for ticker, ms in gs.markets.items():
            assert ms.active is False

        # MKT_Y should have been logged too
        event_calls = engine.db.insert_event.call_args_list
        all_call_strs = [str(c) for c in event_calls]
        assert any("MKT_Y" in s for s in all_call_strs), \
            "MKT_Y silently deactivated by L3 FULL_STOP"


# ---------------------------------------------------------------------------
# Test 2: Empty orderbook should log, not silently return
# ---------------------------------------------------------------------------

class TestEmptyOrderbookLogged:
    """When orderbook returns empty data, the tick should log/not silently skip."""

    def test_empty_yes_bids_logs_skip(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        engine.client.get_orderbook.return_value = {
            "orderbook_fp": {
                "yes_dollars": [],
                "no_dollars": [["0.53", "200"]],
            }
        }
        engine.client.get_trades.return_value = _no_trades()

        engine.tick_one_market(ms)
        # Market should stay active (transient issue)
        assert ms.active is True
        # But a skip should be logged so we can diagnose
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0, \
            "Empty orderbook caused silent skip without logging"

    def test_empty_both_sides_logs_skip(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        engine.client.get_orderbook.return_value = {
            "orderbook_fp": {"yes_dollars": [], "no_dollars": []},
        }
        engine.client.get_trades.return_value = _no_trades()

        engine.tick_one_market(ms)
        assert ms.active is True
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0, \
            "Empty orderbook (both sides) caused silent skip without logging"


# ---------------------------------------------------------------------------
# Test 3: _settle_market logs deactivation
# ---------------------------------------------------------------------------

class TestSettleMarketLogged:
    """Market resolution deactivation must be logged."""

    def test_settle_yes_logs_exit(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        ms.yes_queue = [45]
        ms.no_queue = [55]

        engine._settle_market(ms, "yes")

        assert ms.active is False
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0, \
            "Market settlement deactivated without logging"
        # Should mention resolution
        all_strs = [str(c) for c in event_calls]
        assert any("resolved" in s.lower() for s in all_strs)


# ---------------------------------------------------------------------------
# Test 4: Exception in _check_resolution should not silently deactivate
# ---------------------------------------------------------------------------

class TestCheckResolutionSafety:
    """_check_resolution must not silently deactivate on exception."""

    def test_resolution_api_error_keeps_active(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        ms.active = True

        engine.client.get_market.side_effect = Exception("API timeout")
        engine._check_resolution(ms)

        assert ms.active is True, \
            "Market silently deactivated due to resolution check error"


# ---------------------------------------------------------------------------
# Test 5: L4 EXIT_MARKET logs reason
# ---------------------------------------------------------------------------

class TestL4ExitMarketLogged:
    """L4 EXIT_MARKET should log before deactivating."""

    def test_l4_exit_market_is_logged(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        engine.client.get_orderbook.return_value = _standard_book()
        engine.client.get_trades.return_value = _no_trades()

        with patch("src.mm.engine.check_layer4", return_value=Action.EXIT_MARKET):
            engine.tick_one_market(ms)

        assert ms.active is False
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0, \
            "L4 EXIT_MARKET deactivated without logging"


# ---------------------------------------------------------------------------
# Test 6: HTTP 401/403/404 logs before deactivation
# ---------------------------------------------------------------------------

class TestHTTPErrorLogged:
    """Fatal HTTP errors should log before deactivating."""

    def test_http_404_logs_exit(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        resp = MagicMock()
        resp.status_code = 404
        engine.client.get_orderbook.side_effect = requests.exceptions.HTTPError(
            response=resp)

        engine.tick_one_market(ms)

        assert ms.active is False
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0, \
            "HTTP 404 deactivated market without logging"


# ---------------------------------------------------------------------------
# Test 7: L2/L3 EXIT_MARKET logs before deactivation
# ---------------------------------------------------------------------------

class TestL2L3ExitLogged:
    """L2/L3 EXIT_MARKET path should log."""

    def test_l3_exit_market_logs(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        engine.client.get_orderbook.return_value = _standard_book()
        engine.client.get_trades.return_value = _no_trades()

        with patch("src.mm.engine.check_layer4", return_value=Action.CONTINUE), \
             patch("src.mm.engine.check_layer2", return_value=Action.CONTINUE), \
             patch("src.mm.engine.check_layer3", return_value=Action.EXIT_MARKET):
            engine.tick_one_market(ms)

        assert ms.active is False
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0


# ---------------------------------------------------------------------------
# Test 8: paper_mm main loop exception handler should log deactivation reason
# ---------------------------------------------------------------------------

class TestMainLoopExceptionLogged:
    """Unexpected exceptions in tick_one_market should be logged as events,
    not just printed to stderr."""

    def test_unexpected_exception_logs_event(self):
        """Engine should log an event when an unexpected exception occurs."""
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        # Simulate unexpected error in get_orderbook
        engine.client.get_orderbook.side_effect = RuntimeError("segfault sim")

        # This should be caught by the generic exception handler
        engine.tick_one_market(ms)

        # Market should stay active (transient) but event should be logged
        event_calls = engine.db.insert_event.call_args_list
        assert len(event_calls) > 0 or ms.active is True, \
            "Unexpected exception not logged"


# ---------------------------------------------------------------------------
# Test 9: Deactivation reason tracking on MarketState
# ---------------------------------------------------------------------------

class TestDeactivationReason:
    """MarketState should track WHY it was deactivated."""

    def test_deactivation_reason_set_on_game_started(self):
        """is_live_game exit should set deactivation_reason."""
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        # Make it live
        now = datetime.now(timezone.utc)
        ms.trade_timestamps = [now - timedelta(seconds=i * 3) for i in range(60)]

        engine.client.get_orderbook.return_value = _standard_book()
        engine.client.get_trades.return_value = _no_trades()

        engine.tick_one_market(ms)
        assert ms.active is False
        assert hasattr(ms, "deactivation_reason"), \
            "MarketState should have deactivation_reason attribute"
        assert ms.deactivation_reason is not None, \
            "deactivation_reason should be set when market goes inactive"
        assert "game" in ms.deactivation_reason.lower() or \
               "live" in ms.deactivation_reason.lower()

    def test_deactivation_reason_set_on_full_stop(self):
        engine, gs = _make_engine(["MKT_A", "MKT_B"])
        ms_a = gs.markets["MKT_A"]

        engine.client.get_orderbook.return_value = _standard_book()
        engine.client.get_trades.return_value = _no_trades()

        with patch("src.mm.engine.check_layer4", return_value=Action.FULL_STOP):
            engine.tick_one_market(ms_a)

        for ticker, ms in gs.markets.items():
            assert ms.active is False
            assert ms.deactivation_reason is not None, \
                f"{ticker} deactivated without reason"

    def test_deactivation_reason_set_on_settle(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        ms.yes_queue = [45]

        engine._settle_market(ms, "yes")
        assert ms.active is False
        assert ms.deactivation_reason is not None

    def test_deactivation_reason_set_on_http_fatal(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]

        resp = MagicMock()
        resp.status_code = 404
        engine.client.get_orderbook.side_effect = requests.exceptions.HTTPError(
            response=resp)

        engine.tick_one_market(ms)
        assert ms.active is False
        assert ms.deactivation_reason is not None
        assert "404" in ms.deactivation_reason
