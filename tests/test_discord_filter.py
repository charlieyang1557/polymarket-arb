# tests/test_discord_filter.py
"""Tests for Discord notification filtering.

Discord should ONLY fire for:
- FILL events (maker fills, aggress)
- GAME_STARTED / EXIT_MARKET / orderbook_dead deactivation
- L3 events: PAUSE_30MIN, FULL_STOP
- Session start/end summaries (handled in paper_mm.py, not engine)
- 12-hour periodic summaries (handled in paper_mm.py, not engine)

Discord should NEVER fire for:
- SKIP_TICK (empty orderbook, API errors, crossed book)
- PAUSE_60S (L4 minor pauses)
- DRAIN events
- Normal tick logging
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

from src.mm.state import MarketState, GlobalState, SimOrder
from src.mm.engine import MMEngine, discord_notify
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


def _standard_book():
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.44", "100"], ["0.45", "200"]],
            "no_dollars": [["0.52", "100"], ["0.53", "200"]],
        }
    }


def _no_trades():
    return {"trades": []}


class TestDiscordFilterOnLogEvent:
    """_log_event should only send Discord for critical actions."""

    def test_discord_fires_for_full_stop(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 3, Action.FULL_STOP, "test reason")
            mock_discord.assert_called_once()
            assert "FULL_STOP" in mock_discord.call_args[0][0]

    def test_discord_fires_for_pause_30min(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 3, Action.PAUSE_30MIN, "test reason")
            mock_discord.assert_called_once()

    def test_discord_fires_for_exit_market(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 4, Action.EXIT_MARKET, "test reason")
            mock_discord.assert_called_once()

    def test_discord_not_for_skip_tick(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 4, Action.SKIP_TICK, "empty orderbook")
            mock_discord.assert_not_called()

    def test_discord_not_for_pause_60s(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 4, Action.PAUSE_60S, "spread too wide")
            mock_discord.assert_not_called()

    def test_discord_not_for_continue(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 4, Action.CONTINUE, "normal")
            mock_discord.assert_not_called()

    def test_discord_not_for_cancel_all(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._log_event(ms, 4, Action.CANCEL_ALL, "risk")
            mock_discord.assert_not_called()


class TestDiscordFiresForFills:
    """Discord should fire for maker fills and aggress fills."""

    def test_discord_fires_on_maker_fill(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        order = SimOrder(side="yes", price=45, size=2, remaining=2,
                         queue_pos=0, placed_at=datetime.now(timezone.utc))
        order.db_id = 1
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._record_fill(ms, order, 1, 45, 53)
            mock_discord.assert_called_once()
            assert "Fill" in mock_discord.call_args[0][0]

    def test_discord_fires_on_aggress_flatten(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        ms.yes_queue = [45, 45]  # long YES, net_inventory=2
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine._aggress_flatten(ms, 45, 48, 52, 46.5)
            mock_discord.assert_called_once()
            assert "Aggress" in mock_discord.call_args[0][0]


class TestDiscordNotForSkipTickScenarios:
    """End-to-end: SKIP_TICK from empty book should NOT trigger Discord."""

    def test_empty_book_skip_tick_no_discord(self):
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        engine.client.get_orderbook.return_value = {
            "orderbook_fp": {"yes_dollars": [], "no_dollars": []}
        }
        engine.client.get_trades.return_value = _no_trades()
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine.tick_one_market(ms)
            # Should not fire discord for first skip tick
            mock_discord.assert_not_called()

    def test_transient_http_error_no_discord(self):
        """Transient HTTP errors log SKIP_TICK — no Discord."""
        import requests
        engine, gs = _make_engine(["MKT_A"])
        ms = gs.markets["MKT_A"]
        resp = MagicMock()
        resp.status_code = 500
        engine.client.get_orderbook.side_effect = requests.exceptions.HTTPError(
            response=resp)
        with patch("src.mm.engine.discord_notify") as mock_discord:
            engine.tick_one_market(ms)
            mock_discord.assert_not_called()
