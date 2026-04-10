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


class TestProgressiveExitPrice:
    """progressive_exit_price: time-decayed exit pricing for SOFT_CLOSE."""

    def test_30min_out_tries_profit(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=1800)
        assert price == 49

    def test_20min_out_breakeven(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=1200)
        assert price == 50

    def test_10min_out_accept_loss(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=600)
        assert price == 52

    def test_5min_out_larger_loss(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=300)
        assert price == 53

    def test_2min_out_taker_cross(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=90)
        assert price == 52

    def test_no_side_fair_value(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="no", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=1200)
        assert price == 50

    def test_clamped_to_1_99(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=98.0, best_bid=97,
            best_ask=99, seconds_to_game=600)
        assert price is not None
        assert price <= 99

    def test_max_slippage_cap(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=60, seconds_to_game=600,
            max_slippage=5)
        assert price is not None
        assert price <= 55

    def test_wide_book_returns_none(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=35,
            best_ask=65, seconds_to_game=90,
            max_taker_loss=10)
        assert price is None

    def test_wide_book_exact_boundary_crosses(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=40,
            best_ask=60, seconds_to_game=90,
            max_taker_loss=10)
        assert price is not None
        assert price == 55  # best_ask(60)+1=61 but capped at fair(50)+max_slippage(5)=55

    def test_empty_book_returns_none(self):
        from src.mm.engine import progressive_exit_price
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=0, seconds_to_game=90,
            max_taker_loss=10)
        assert price is None

    def test_custom_ladder(self):
        from src.mm.engine import progressive_exit_price
        from src.mm.state import ExitLadderStep
        custom = (ExitLadderStep(seconds_threshold=9999, price_offset=10),)
        price = progressive_exit_price(
            side="yes", fair_value=50.0, best_bid=49,
            best_ask=51, seconds_to_game=5000,
            ladder=custom)
        assert price == 55  # 50+10=60, capped at 50+5=55

    def test_legacy_soft_close_exit_price_still_works(self):
        from src.mm.engine import soft_close_exit_price
        price = soft_close_exit_price(
            side="yes", fair_value=50.0, best_bid=49, max_slippage=5)
        assert 49 <= price <= 55


class TestReducedMakingSideSize:
    """clamp_order_size reduces making side proportionally to |inv|."""

    def test_making_side_reduced_at_inv_1(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("yes", net_inventory=1, order_size=2,
                                max_inventory=10)
        assert size == 1

    def test_making_side_reduced_at_inv_2(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("yes", net_inventory=2, order_size=2,
                                max_inventory=10)
        assert size == 1

    def test_reducing_side_keeps_full_size(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("no", net_inventory=2, order_size=2,
                                max_inventory=10)
        assert size == 2

    def test_flat_inventory_full_size(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("yes", net_inventory=0, order_size=2,
                                max_inventory=10)
        assert size == 2

    def test_making_side_never_below_1(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("yes", net_inventory=5, order_size=2,
                                max_inventory=10)
        assert size == 1

    def test_still_zero_at_max_inventory(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("yes", net_inventory=10, order_size=2,
                                max_inventory=10)
        assert size == 0

    def test_no_side_making_when_short(self):
        from src.mm.engine import clamp_order_size
        size = clamp_order_size("no", net_inventory=-2, order_size=2,
                                max_inventory=10)
        assert size == 1


class TestNearMidDepthFilter:
    """Scanner requires >=3 contracts within 3c of mid on both sides."""

    def test_passes_with_sufficient_depth(self):
        from scripts.poly_daily_scan import apply_prefilters
        c = {"spread": 3, "midpoint": 50, "net_spread": 2.5,
             "best_yes_depth": 10, "best_no_depth": 10,
             "symmetry": 1.0,
             "near_mid_yes_depth": 5, "near_mid_no_depth": 5}
        assert apply_prefilters(c) is True

    def test_fails_with_thin_yes_depth(self):
        from scripts.poly_daily_scan import apply_prefilters
        c = {"spread": 3, "midpoint": 50, "net_spread": 2.5,
             "best_yes_depth": 10, "best_no_depth": 10,
             "symmetry": 1.0,
             "near_mid_yes_depth": 2, "near_mid_no_depth": 5}
        assert apply_prefilters(c) is False

    def test_fails_with_thin_no_depth(self):
        from scripts.poly_daily_scan import apply_prefilters
        c = {"spread": 3, "midpoint": 50, "net_spread": 2.5,
             "best_yes_depth": 10, "best_no_depth": 10,
             "symmetry": 1.0,
             "near_mid_yes_depth": 5, "near_mid_no_depth": 1}
        assert apply_prefilters(c) is False

    def test_backward_compat_missing_field(self):
        from scripts.poly_daily_scan import apply_prefilters
        c = {"spread": 3, "midpoint": 50, "net_spread": 2.5,
             "best_yes_depth": 10, "best_no_depth": 10,
             "symmetry": 1.0}
        assert apply_prefilters(c) is True
