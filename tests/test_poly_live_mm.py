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
    """MIN_REQUOTE_DELTA = 2c. Only requote if |target - current| >= 2."""

    def test_no_requote_same_price(self):
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=42, current_price=42) is False

    def test_no_requote_1c_diff(self):
        from scripts.poly_live_mm import should_requote
        # 1c diff < MIN_REQUOTE_DELTA (2) → no requote, preserve queue
        assert should_requote(target_price=43, current_price=42) is False

    def test_requote_at_threshold(self):
        from scripts.poly_live_mm import should_requote
        # Exactly 2c diff → requote
        assert should_requote(target_price=44, current_price=42) is True

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


# ---------------------------------------------------------------------------
# Test: local order tracking — prevents requote thrashing
# ---------------------------------------------------------------------------

class TestLocalOrderTracking:
    """After place_order, local tracking must prevent needless cancel+replace.

    Bug: poll_open_orders() can return empty (API lag, format mismatch),
    making `existing` always None in _manage_live_quotes, bypassing
    should_requote and placing a new order every tick.

    Fix: LiveOrderManager tracks placed orders locally. merged_orders()
    combines poll results with local state so orders are never "lost".
    """

    def _make_manager(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)
        return mgr

    def test_place_order_populates_local_orders(self):
        """After place_order, _local_orders should contain the order info."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        assert "slug-a" in mgr._local_orders
        assert "yes" in mgr._local_orders["slug-a"]
        info = mgr._local_orders["slug-a"]["yes"]
        assert info["price_cents"] == 48
        assert info["remaining_qty"] == 2

    def test_cancel_order_removes_local_orders(self):
        """After cancel_order, _local_orders should remove that side."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        assert "yes" in mgr._local_orders.get("slug-a", {})
        mgr.cancel_order("slug-a", "yes", "dry-abc123")
        assert "yes" not in mgr._local_orders.get("slug-a", {})

    def test_merged_orders_uses_local_when_poll_empty(self):
        """When poll returns empty, merged_orders should use local state."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        mgr.place_order("slug-a", "no", 50, 2)
        # Simulate poll returning nothing (API lag)
        polled = {}
        merged = mgr.merged_orders(polled)
        assert "slug-a" in merged
        assert merged["slug-a"]["yes"]["price_cents"] == 48
        assert merged["slug-a"]["no"]["price_cents"] == 50

    def test_merged_orders_exchange_truth_wins(self):
        """When poll returns data, exchange truth takes precedence."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        # Simulate poll showing partial fill (exchange truth)
        polled = {"slug-a": {"yes": {
            "order_id": "real-id",
            "price_cents": 48,
            "original_qty": 2,
            "filled_qty": 1,
            "remaining_qty": 1,
        }}}
        merged = mgr.merged_orders(polled)
        # Exchange truth should win
        assert merged["slug-a"]["yes"]["filled_qty"] == 1
        assert merged["slug-a"]["yes"]["remaining_qty"] == 1

    def test_merged_orders_preserves_polled_only_slugs(self):
        """Slugs only in poll (not local) should pass through."""
        mgr = self._make_manager()
        polled = {"slug-b": {"yes": {
            "order_id": "ext-1", "price_cents": 55,
            "original_qty": 3, "filled_qty": 0, "remaining_qty": 3,
        }}}
        merged = mgr.merged_orders(polled)
        assert merged["slug-b"]["yes"]["price_cents"] == 55

    def test_cancel_all_clears_local_orders(self):
        """cancel_all_orders should also clear local tracking."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        mgr.place_order("slug-b", "no", 50, 2)
        mgr.cancel_all_orders()
        assert mgr._local_orders == {}

    def test_poll_remaps_api_slug_to_our_slug(self):
        """poll_open_orders remaps API marketSlug to our slug via order_id."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Simulate a real place_order that stored order_id in _live_order_ids
        mgr._live_order_ids["my-slug-123"] = {"yes": "ord-AAA"}

        # API returns the same order but under a different marketSlug
        client.list_orders.return_value = {"orders": [{
            "id": "ord-AAA",
            "marketSlug": "api-slug-xyz",
            "intent": "ORDER_INTENT_BUY_LONG",
            "price": {"value": "0.480", "currency": "USD"},
            "quantity": 2,
            "cumQuantity": 0,
            "leavesQuantity": 2,
            "state": "ORDER_STATE_NEW",
        }]}

        result = mgr.poll_open_orders(["my-slug-123"])
        # Should be keyed by OUR slug, not the API's
        assert "my-slug-123" in result
        assert "api-slug-xyz" not in result
        assert result["my-slug-123"]["yes"]["order_id"] == "ord-AAA"

    def test_poll_remap_preserves_unknown_orders(self):
        """Orders not in _live_order_ids keep their API slug."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # No _live_order_ids — order from API should keep its slug
        client.list_orders.return_value = {"orders": [{
            "id": "ord-unknown",
            "marketSlug": "some-api-slug",
            "intent": "ORDER_INTENT_BUY_SHORT",
            "price": {"value": "0.500", "currency": "USD"},
            "quantity": 3,
            "cumQuantity": 0,
            "leavesQuantity": 3,
            "state": "ORDER_STATE_NEW",
        }]}

        result = mgr.poll_open_orders(["some-api-slug"])
        assert "some-api-slug" in result
        assert result["some-api-slug"]["no"]["order_id"] == "ord-unknown"


# ---------------------------------------------------------------------------
# Test: MIN_REQUOTE_DELTA — sticky quotes preserve queue priority
# ---------------------------------------------------------------------------

class TestMinRequoteDelta:
    """MIN_REQUOTE_DELTA = 2c. Don't cancel+replace unless price moves >= 2c.

    Root cause: 1c spread markets where every requote (cancel+replace)
    sends us to back of queue. Bot requoted 27 times in a live session,
    destroying queue priority each time. Zero fills in 9 hours.
    """

    def test_1c_move_no_requote(self):
        """Price moves 1c → delta < MIN_REQUOTE_DELTA (2) → keep existing."""
        from scripts.poly_live_mm import should_requote, MIN_REQUOTE_DELTA
        assert MIN_REQUOTE_DELTA == 2
        assert should_requote(target_price=43, current_price=42) is False

    def test_2c_move_triggers_requote(self):
        """Price moves 2c → delta >= MIN_REQUOTE_DELTA → requote."""
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=44, current_price=42) is True

    def test_0c_move_no_requote(self):
        """Same price → no requote (already covered, but verify with new delta)."""
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=42, current_price=42) is False

    def test_3c_move_triggers_requote(self):
        """Large move → requote."""
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=45, current_price=42) is True

    def test_negative_2c_move_triggers_requote(self):
        """Price drops 2c → requote."""
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=40, current_price=42) is True

    def test_negative_1c_move_no_requote(self):
        """Price drops 1c → no requote."""
        from scripts.poly_live_mm import should_requote
        assert should_requote(target_price=41, current_price=42) is False


class TestShouldRequoteWithOverrides:
    """Test should_requote_or_force — respects MIN_REQUOTE_DELTA but allows
    force-requote on soft-close, first placement, and inventory change."""

    def test_soft_close_always_requotes(self):
        """SOFT_CLOSE mode → always requote even if delta < 2."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(
            target_price=43, current_price=42,
            force_requote=True) is True

    def test_first_placement_always_places(self):
        """No existing order → always place (existing=None path, not tested
        via should_requote_or_force but verified in integration)."""
        # First placement is handled by checking existing is None before
        # calling should_requote, so this tests the force flag pathway
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(
            target_price=43, current_price=42,
            force_requote=True) is True

    def test_inventory_change_forces_requote(self):
        """Fill detected (inventory changed) → force requote even if delta < 2."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(
            target_price=43, current_price=42,
            force_requote=True) is True

    def test_no_force_respects_delta(self):
        """Normal tick, no force → respects MIN_REQUOTE_DELTA."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(
            target_price=43, current_price=42,
            force_requote=False) is False

    def test_no_force_large_delta_requotes(self):
        """Normal tick with large delta → requote."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(
            target_price=44, current_price=42,
            force_requote=False) is True
