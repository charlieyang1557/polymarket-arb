"""Tests for poly_live_mm.py — live order management for Polymarket US."""

import pytest
import sys
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Test: should_requote — determines whether to cancel+replace an order
# ---------------------------------------------------------------------------

class TestShouldRequote:
    """REQUOTE_TOL = 1c. Only requote if |target - current| >= REQUOTE_TOL."""

    def test_no_requote_same_price(self):
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=42, current_price=42) is False

    def test_requote_at_threshold(self):
        from scripts.poly_live_mm import should_requote
        # Exactly 1c diff → requote
        assert should_requote(target_price=43, current_price=42) is True

    def test_no_requote_below_threshold(self):
        from scripts.poly_live_mm import should_requote
        # 0c diff — same price
        assert should_requote(target_price=42, current_price=42) is False

    def test_requote_large_diff(self):
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=50, current_price=42) is True

    def test_requote_negative_diff(self):
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=40, current_price=42) is True


# ---------------------------------------------------------------------------
# Test: max_order_value_check — 5% capital cap per order
# ---------------------------------------------------------------------------

class TestMaxOrderValue:
    """Max single order value = capital * 0.05."""

    def test_within_limit(self):
        from scripts.poly_live_mm import max_order_value_check
        # $25 capital (2500c), order: 2 contracts @ 50c = 100c value
        # 5% of 2500 = 125c → OK
        assert max_order_value_check(
            price_cents=50, count=2, capital_cents=2500) is True

    def test_at_limit(self):
        from scripts.poly_live_mm import max_order_value_check
        # 5 contracts @ 25c = 125c, limit = 125c → OK
        assert max_order_value_check(
            price_cents=25, count=5, capital_cents=2500) is True

    def test_over_limit(self):
        from scripts.poly_live_mm import max_order_value_check
        # 5 contracts @ 50c = 250c, limit = 125c → REJECT
        assert max_order_value_check(
            price_cents=50, count=5, capital_cents=2500) is False

    def test_large_capital(self):
        from scripts.poly_live_mm import max_order_value_check
        # $200 capital (20000c), 10 contracts @ 50c = 500c
        # 5% of 20000 = 1000c → OK
        assert max_order_value_check(
            price_cents=50, count=10, capital_cents=20000) is True


# ---------------------------------------------------------------------------
# Test: parse_open_orders — normalize API response to internal format
# ---------------------------------------------------------------------------

class TestParseOpenOrders:
    """Parse SDK order list response into {slug: {side: order_info}} map."""

    def test_empty_response(self):
        from scripts.poly_live_mm import parse_open_orders
        result = parse_open_orders({"orders": []})
        assert result == {}

    def test_single_order(self):
        from scripts.poly_live_mm import parse_open_orders
        resp = {"orders": [{
            "id": "ord-123",
            "marketSlug": "nba-lakers-celtics",
            "intent": "ORDER_INTENT_BUY_LONG",
            "price": {"value": "0.450", "currency": "USD"},
            "quantity": 5,
            "cumQuantity": 2,
            "leavesQuantity": 3,
            "state": "ORDER_STATE_PARTIALLY_FILLED",
        }]}
        result = parse_open_orders(resp)
        assert "nba-lakers-celtics" in result
        info = result["nba-lakers-celtics"]["yes"]
        assert info["order_id"] == "ord-123"
        assert info["price_cents"] == 45
        assert info["original_qty"] == 5
        assert info["filled_qty"] == 2
        assert info["remaining_qty"] == 3

    def test_buy_short_is_no_side(self):
        from scripts.poly_live_mm import parse_open_orders
        resp = {"orders": [{
            "id": "ord-456",
            "marketSlug": "nba-game",
            "intent": "ORDER_INTENT_BUY_SHORT",
            "price": {"value": "0.380", "currency": "USD"},
            "quantity": 3,
            "cumQuantity": 0,
            "leavesQuantity": 3,
            "state": "ORDER_STATE_NEW",
        }]}
        result = parse_open_orders(resp)
        info = result["nba-game"]["no"]
        assert info["price_cents"] == 38

    def test_multiple_slugs_and_sides(self):
        from scripts.poly_live_mm import parse_open_orders
        resp = {"orders": [
            {
                "id": "ord-1", "marketSlug": "slug-a",
                "intent": "ORDER_INTENT_BUY_LONG",
                "price": {"value": "0.500", "currency": "USD"},
                "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
                "state": "ORDER_STATE_NEW",
            },
            {
                "id": "ord-2", "marketSlug": "slug-a",
                "intent": "ORDER_INTENT_BUY_SHORT",
                "price": {"value": "0.480", "currency": "USD"},
                "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
                "state": "ORDER_STATE_NEW",
            },
            {
                "id": "ord-3", "marketSlug": "slug-b",
                "intent": "ORDER_INTENT_BUY_LONG",
                "price": {"value": "0.600", "currency": "USD"},
                "quantity": 3, "cumQuantity": 1, "leavesQuantity": 2,
                "state": "ORDER_STATE_PARTIALLY_FILLED",
            },
        ]}
        result = parse_open_orders(resp)
        assert len(result) == 2
        assert "yes" in result["slug-a"]
        assert "no" in result["slug-a"]
        assert result["slug-b"]["yes"]["filled_qty"] == 1


# ---------------------------------------------------------------------------
# Test: detect_fills — compare prev vs current cumQuantity
# ---------------------------------------------------------------------------

class TestDetectFills:
    """Track fills by comparing cumQuantity between ticks."""

    def test_no_fill(self):
        from scripts.poly_live_mm import detect_fills
        prev = {"order_id": "o1", "price_cents": 50, "filled_qty": 0,
                "remaining_qty": 5, "original_qty": 5}
        curr = {"order_id": "o1", "price_cents": 50, "filled_qty": 0,
                "remaining_qty": 5, "original_qty": 5}
        fills = detect_fills(prev, curr)
        assert fills == 0

    def test_partial_fill(self):
        from scripts.poly_live_mm import detect_fills
        prev = {"order_id": "o1", "price_cents": 50, "filled_qty": 0,
                "remaining_qty": 5, "original_qty": 5}
        curr = {"order_id": "o1", "price_cents": 50, "filled_qty": 2,
                "remaining_qty": 3, "original_qty": 5}
        fills = detect_fills(prev, curr)
        assert fills == 2

    def test_full_fill(self):
        from scripts.poly_live_mm import detect_fills
        prev = {"order_id": "o1", "price_cents": 50, "filled_qty": 2,
                "remaining_qty": 3, "original_qty": 5}
        curr = {"order_id": "o1", "price_cents": 50, "filled_qty": 5,
                "remaining_qty": 0, "original_qty": 5}
        fills = detect_fills(prev, curr)
        assert fills == 3

    def test_different_order_id_returns_zero(self):
        """If order was replaced, don't double-count fills."""
        from scripts.poly_live_mm import detect_fills
        prev = {"order_id": "o1", "price_cents": 50, "filled_qty": 2,
                "remaining_qty": 3, "original_qty": 5}
        curr = {"order_id": "o2", "price_cents": 51, "filled_qty": 0,
                "remaining_qty": 5, "original_qty": 5}
        fills = detect_fills(prev, curr)
        assert fills == 0

    def test_none_prev_returns_zero(self):
        from scripts.poly_live_mm import detect_fills
        curr = {"order_id": "o1", "price_cents": 50, "filled_qty": 2,
                "remaining_qty": 3, "original_qty": 5}
        fills = detect_fills(None, curr)
        assert fills == 0


# ---------------------------------------------------------------------------
# Test: parse_positions — sync exchange positions to local state
# ---------------------------------------------------------------------------

class TestParsePositions:
    """Parse portfolio positions response to {slug: net_position} map."""

    def test_empty_positions(self):
        from scripts.poly_live_mm import parse_positions
        result = parse_positions({"positions": {}})
        assert result == {}

    def test_long_position(self):
        from scripts.poly_live_mm import parse_positions
        resp = {"positions": {
            "nba-game": {
                "netPosition": "5",
                "qtyBought": "10",
                "qtySold": "5",
                "cost": {"value": "2.50", "currency": "USD"},
            }
        }}
        result = parse_positions(resp)
        assert result["nba-game"] == 5

    def test_short_position(self):
        from scripts.poly_live_mm import parse_positions
        resp = {"positions": {
            "nba-game": {
                "netPosition": "-3",
                "qtyBought": "2",
                "qtySold": "5",
                "cost": {"value": "-1.50", "currency": "USD"},
            }
        }}
        result = parse_positions(resp)
        assert result["nba-game"] == -3

    def test_zero_position_excluded(self):
        from scripts.poly_live_mm import parse_positions
        resp = {"positions": {
            "nba-game": {
                "netPosition": "0",
                "qtyBought": "5",
                "qtySold": "5",
                "cost": {"value": "0.00", "currency": "USD"},
            }
        }}
        result = parse_positions(resp)
        assert "nba-game" not in result


# ---------------------------------------------------------------------------
# Test: compute_risk_params (reused from paper)
# ---------------------------------------------------------------------------

class TestComputeRiskParams:
    def test_small_capital(self):
        from scripts.poly_live_mm import compute_risk_params
        r = compute_risk_params(2500)  # $25
        assert r["max_inventory"] == 10
        assert r["max_unhedged_exit"] >= 2
        assert r["aggress_threshold"] >= 2

    def test_large_capital(self):
        from scripts.poly_live_mm import compute_risk_params
        r = compute_risk_params(20000)  # $200
        assert r["max_inventory"] == 80
        assert r["max_unhedged_exit"] == 40


# ---------------------------------------------------------------------------
# Test: order intent mapping
# ---------------------------------------------------------------------------

class TestOrderIntent:
    def test_yes_side_is_buy_long(self):
        from scripts.poly_live_mm import side_to_intent
        assert side_to_intent("yes") == "ORDER_INTENT_BUY_LONG"

    def test_no_side_is_buy_short(self):
        from scripts.poly_live_mm import side_to_intent
        assert side_to_intent("no") == "ORDER_INTENT_BUY_SHORT"

    def test_intent_to_side_buy_long(self):
        from scripts.poly_live_mm import intent_to_side
        assert intent_to_side("ORDER_INTENT_BUY_LONG") == "yes"

    def test_intent_to_side_buy_short(self):
        from scripts.poly_live_mm import intent_to_side
        assert intent_to_side("ORDER_INTENT_BUY_SHORT") == "no"


# ---------------------------------------------------------------------------
# Test: clamp_price — strict price bounds [1, 99]
# ---------------------------------------------------------------------------

class TestClampPrice:
    """Prices must be strictly between $0.01 and $0.99 inclusive."""

    def test_normal_price_unchanged(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(50) == 50

    def test_zero_clamped_to_one(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(0) == 1

    def test_negative_clamped_to_one(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(-5) == 1

    def test_hundred_clamped_to_99(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(100) == 99

    def test_over_hundred_clamped(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(120) == 99

    def test_one_is_valid(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(1) == 1

    def test_99_is_valid(self):
        from scripts.poly_live_mm import clamp_price
        assert clamp_price(99) == 99
