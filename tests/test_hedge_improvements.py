"""Tests for hedging improvement functions."""

import pytest
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestHedgeUrgencyOffset:
    """hedge_urgency_offset: returns price improvement in cents based on
    elapsed time since oldest unhedged fill."""

    def test_no_fill_returns_zero(self):
        from src.mm.state import hedge_urgency_offset
        assert hedge_urgency_offset(None) == 0

    def test_within_5min_passive(self):
        from src.mm.state import hedge_urgency_offset
        now = datetime.now(timezone.utc)
        fill_time = now - timedelta(minutes=3)
        assert hedge_urgency_offset(fill_time, now=now) == 0

    def test_at_5min_boundary(self):
        from src.mm.state import hedge_urgency_offset
        now = datetime.now(timezone.utc)
        fill_time = now - timedelta(minutes=5)
        assert hedge_urgency_offset(fill_time, now=now) == 1

    def test_at_10min(self):
        from src.mm.state import hedge_urgency_offset
        now = datetime.now(timezone.utc)
        fill_time = now - timedelta(minutes=10)
        assert hedge_urgency_offset(fill_time, now=now) == 2

    def test_at_15min_taker_threshold(self):
        from src.mm.state import hedge_urgency_offset
        now = datetime.now(timezone.utc)
        fill_time = now - timedelta(minutes=15)
        assert hedge_urgency_offset(fill_time, now=now) == 5

    def test_at_20min_still_5(self):
        from src.mm.state import hedge_urgency_offset
        now = datetime.now(timezone.utc)
        fill_time = now - timedelta(minutes=20)
        assert hedge_urgency_offset(fill_time, now=now) == 5

    def test_at_exactly_0_seconds(self):
        from src.mm.state import hedge_urgency_offset
        now = datetime.now(timezone.utc)
        assert hedge_urgency_offset(now, now=now) == 0


class TestWidenedSoftClose:
    """SOFT_CLOSE should trigger at 30 min (1800s) instead of 15 min (900s)."""

    def test_soft_close_at_25min(self):
        from src.mm.risk import check_layer4, Action
        from src.mm.state import MarketState
        ms = MarketState(ticker="test")
        now = datetime.now(timezone.utc)
        ms.game_start_utc = now + timedelta(minutes=25)
        ms.last_api_success = now
        result = check_layer4(ms, spread=3, db_error_count=0)
        assert result == Action.SOFT_CLOSE

    def test_no_soft_close_at_35min(self):
        from src.mm.risk import check_layer4, Action
        from src.mm.state import MarketState
        ms = MarketState(ticker="test")
        now = datetime.now(timezone.utc)
        ms.game_start_utc = now + timedelta(minutes=35)
        ms.last_api_success = now
        result = check_layer4(ms, spread=3, db_error_count=0)
        assert result == Action.CONTINUE

    def test_exit_market_at_game_start(self):
        from src.mm.risk import check_layer4, Action
        from src.mm.state import MarketState
        ms = MarketState(ticker="test")
        now = datetime.now(timezone.utc)
        ms.game_start_utc = now - timedelta(minutes=1)
        ms.last_api_success = now
        result = check_layer4(ms, spread=3, db_error_count=0)
        assert result == Action.EXIT_MARKET
