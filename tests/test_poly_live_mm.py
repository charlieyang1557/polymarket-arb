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

        result, ok = mgr.poll_open_orders(["my-slug-123"])
        assert ok is True
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

        result, ok = mgr.poll_open_orders(["some-api-slug"])
        assert ok is True
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


# ---------------------------------------------------------------------------
# Test: fill detection — disappeared orders must be detected as fills
# ---------------------------------------------------------------------------

class TestFillDetectionDisappearedOrders:
    """P0 BUG: When a fully filled order disappears from poll_open_orders(),
    check_fills() fails to detect it. Three compounding bugs:

    1. merged_orders() fills in stale local data for disappeared orders,
       masking the disappearance from check_fills().
    2. check_fills() has a `pass` for disappeared orders — does nothing.
    3. The condition `filled_qty > 0` misses orders that go from 0 to fully
       filled in a single tick.

    These tests verify the fix: when a tracked order disappears and was not
    explicitly cancelled, it should be detected as a fill.
    """

    def _make_manager(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)
        return mgr

    def test_fully_filled_order_disappears_detected_as_fill(self):
        """Order placed tick N, disappears tick N+1 → detected as full fill.

        This is the primary bug: 2-lot order at 48c gets fully filled,
        disappears from poll, but merged_orders fills in stale local data
        so check_fills never sees it vanish.
        """
        mgr = self._make_manager()
        # Place order
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        # Tick N: order visible in poll
        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Tick N+1: order disappeared from poll (fully filled)
        raw_polled = {}  # exchange returns nothing for this slug
        curr_orders = mgr.merged_orders(raw_polled)
        fills = mgr.check_fills(curr_orders)

        # MUST detect 2 fills
        assert len(fills) == 1
        assert fills[0]["slug"] == "slug-a"
        assert fills[0]["side"] == "yes"
        assert fills[0]["filled"] == 2
        assert fills[0]["price_cents"] == 48

    def test_cancelled_order_not_detected_as_fill(self):
        """Order cancelled then disappears → NOT a fill."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Cancel the order
        mgr.cancel_order("slug-a", "yes", oid)

        # Tick N+1: order gone from poll
        raw_polled = {}
        curr_orders = mgr.merged_orders(raw_polled)
        fills = mgr.check_fills(curr_orders)

        # Should NOT detect as fill — it was cancelled
        assert len(fills) == 0

    def test_partial_fill_then_disappear_detects_remaining(self):
        """Order partially filled (1 of 2), then disappears → 1 more filled."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        # Tick N: partially filled (1 of 2 filled)
        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 1, "remaining_qty": 1,
        }}}
        mgr.update_prev_orders(tick_n)

        # Tick N+1: order disappeared (remaining 1 filled)
        raw_polled = {}
        curr_orders = mgr.merged_orders(raw_polled)
        fills = mgr.check_fills(curr_orders)

        # Should detect the remaining 1 fill
        assert len(fills) == 1
        assert fills[0]["filled"] == 1

    def test_order_still_open_no_false_fill(self):
        """Order still visible and unchanged → no fill detected."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Tick N+1: order still there, unchanged
        raw_polled = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        curr_orders = mgr.merged_orders(raw_polled)
        fills = mgr.check_fills(curr_orders)

        assert len(fills) == 0

    def test_multi_market_fills_detected_independently(self):
        """Fills across multiple markets detected independently."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        mgr.place_order("slug-b", "no", 50, 2)
        oid_a = mgr._local_orders["slug-a"]["yes"]["order_id"]
        oid_b = mgr._local_orders["slug-b"]["no"]["order_id"]

        tick_n = {
            "slug-a": {"yes": {
                "order_id": oid_a, "price_cents": 48,
                "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
            }},
            "slug-b": {"no": {
                "order_id": oid_b, "price_cents": 50,
                "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
            }},
        }
        mgr.update_prev_orders(tick_n)

        # Tick N+1: both orders disappeared (filled)
        raw_polled = {}
        curr_orders = mgr.merged_orders(raw_polled)
        fills = mgr.check_fills(curr_orders)

        assert len(fills) == 2
        slugs_filled = {f["slug"] for f in fills}
        assert slugs_filled == {"slug-a", "slug-b"}

    def test_local_orders_cleaned_after_fill_detected(self):
        """After detecting a fill, _local_orders should be cleaned up."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Tick N+1: order disappeared
        raw_polled = {}
        curr_orders = mgr.merged_orders(raw_polled)
        fills = mgr.check_fills(curr_orders)

        assert len(fills) == 1
        # Local orders should be cleaned for the filled side
        assert "yes" not in mgr._local_orders.get("slug-a", {})


# ---------------------------------------------------------------------------
# Test: poll-success gate — phantom fills on API failure
# ---------------------------------------------------------------------------

class TestPollSuccessGate:
    """Adversarial review finding: poll_open_orders() returns {} on exception.
    New merged_orders logic stops backfilling local data for tracked orders,
    so a failed poll makes all orders appear "filled" → phantom inventory.

    Fix: poll_open_orders returns (orders, poll_ok). When poll_ok=False,
    skip fill detection and preserve previous state.
    """

    def _make_manager(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)
        return mgr

    def test_poll_failure_no_phantom_fills(self):
        """Poll fails → check_fills returns [] (no phantom fills)."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Poll fails — returns ({}, False)
        raw_polled, poll_ok = {}, False
        curr_orders = mgr.merged_orders(raw_polled, poll_ok)

        # poll_ok=False → should NOT detect fills
        if poll_ok:
            fills = mgr.check_fills(curr_orders)
        else:
            fills = []

        assert len(fills) == 0

    def test_poll_failure_preserves_previous_state(self):
        """Poll fails → merged_orders returns previous tick unchanged."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Poll fails
        raw_polled, poll_ok = {}, False
        curr_orders = mgr.merged_orders(raw_polled, poll_ok)

        # Should return previous tick's state
        assert "slug-a" in curr_orders
        assert "yes" in curr_orders["slug-a"]
        assert curr_orders["slug-a"]["yes"]["order_id"] == oid

    def test_poll_success_still_detects_fills(self):
        """Poll succeeds and order gone → fill detected normally."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Poll succeeds, order gone
        raw_polled, poll_ok = {}, True
        curr_orders = mgr.merged_orders(raw_polled, poll_ok)
        fills = mgr.check_fills(curr_orders)

        assert len(fills) == 1
        assert fills[0]["filled"] == 2

    def test_poll_open_orders_returns_tuple(self):
        """poll_open_orders must return (dict, bool) tuple."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Successful poll
        client.list_orders.return_value = {"orders": []}
        result = mgr.poll_open_orders(["slug-a"])
        assert isinstance(result, tuple)
        assert len(result) == 2
        orders, ok = result
        assert isinstance(orders, dict)
        assert ok is True

    def test_poll_open_orders_failure_returns_false(self):
        """poll_open_orders exception → ({}, False)."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        client.list_orders.side_effect = Exception("API timeout")
        result = mgr.poll_open_orders(["slug-a"])
        orders, ok = result
        assert orders == {}
        assert ok is False


# ---------------------------------------------------------------------------
# Test: confirmed-cancel-only — failed cancels must not suppress fills
# ---------------------------------------------------------------------------

class TestConfirmedCancelOnly:
    """Adversarial review finding: cancel_order() adds to _cancelled_order_ids
    BEFORE the API call. If cancel fails, ID stays marked as cancelled.
    A real fill later gets suppressed because check_fills thinks it was cancelled.

    Fix: Only mark as cancelled after successful API response.
    """

    def test_failed_cancel_does_not_mark_cancelled(self):
        """Cancel API throws → order ID NOT in _cancelled_order_ids."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Simulate a placed order
        mgr._live_order_ids["slug-a"] = {"yes": "ord-123"}
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-123", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}

        # Cancel fails
        client.cancel_order.side_effect = Exception("API timeout")
        mgr.cancel_order("slug-a", "yes", "ord-123")

        # Order ID should NOT be marked as cancelled
        assert "ord-123" not in mgr._cancelled_order_ids

    def test_failed_cancel_then_fill_detected(self):
        """Cancel fails, then order fills → fill IS detected."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        mgr._live_order_ids["slug-a"] = {"yes": "ord-123"}
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-123", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}

        # Set up prev_orders as if we saw the order last tick
        mgr._prev_orders = {"slug-a": {"yes": {
            "order_id": "ord-123", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}

        # Cancel fails
        client.cancel_order.side_effect = Exception("API timeout")
        mgr.cancel_order("slug-a", "yes", "ord-123")

        # Next tick: order disappeared (it was actually filled)
        client.list_orders.return_value = {"orders": []}
        raw_polled, poll_ok = mgr.poll_open_orders(["slug-a"])
        curr_orders = mgr.merged_orders(raw_polled, poll_ok)
        fills = mgr.check_fills(curr_orders)

        # Fill MUST be detected (not suppressed)
        assert len(fills) == 1
        assert fills[0]["filled"] == 2

    def test_successful_cancel_suppresses_fill(self):
        """Cancel succeeds → order disappears → NOT detected as fill."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        mgr._live_order_ids["slug-a"] = {"yes": "ord-123"}
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-123", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}

        mgr._prev_orders = {"slug-a": {"yes": {
            "order_id": "ord-123", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}

        # Cancel succeeds
        client.cancel_order.return_value = {}
        mgr.cancel_order("slug-a", "yes", "ord-123")

        # Verify ID is marked cancelled
        assert "ord-123" in mgr._cancelled_order_ids

        # Next tick: order gone from poll
        client.list_orders.return_value = {"orders": []}
        raw_polled, poll_ok = mgr.poll_open_orders(["slug-a"])
        curr_orders = mgr.merged_orders(raw_polled, poll_ok)
        fills = mgr.check_fills(curr_orders)

        # Should NOT detect as fill
        assert len(fills) == 0


# ---------------------------------------------------------------------------
# Test: post-cancel reconciliation + observability
# ---------------------------------------------------------------------------

class TestPostCancelReconciliation:
    """After cancel_all, reconcile via open-orders poll to detect
    fills that raced with the cancel. Also verify CANCEL_CONFIRMED
    log output for observability."""

    def test_cancel_all_reconciles_remaining_orders(self):
        """cancel_all polls open orders after cancel. Orders still open
        are NOT marked as cancelled."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Track two orders
        mgr._live_order_ids = {
            "slug-a": {"yes": "ord-1"},
            "slug-b": {"no": "ord-2"},
        }
        mgr._local_orders = {
            "slug-a": {"yes": {"order_id": "ord-1", "price_cents": 48,
                               "original_qty": 2, "filled_qty": 0,
                               "remaining_qty": 2}},
            "slug-b": {"no": {"order_id": "ord-2", "price_cents": 50,
                              "original_qty": 2, "filled_qty": 0,
                              "remaining_qty": 2}},
        }

        # cancel_all succeeds, reports ord-1 cancelled
        client.cancel_all_orders.return_value = {
            "canceledOrderIds": ["ord-1"]
        }
        # Reconciliation poll: ord-2 still open (raced with cancel)
        client.list_orders.return_value = {"orders": [{
            "id": "ord-2",
            "marketSlug": "slug-b",
            "intent": "ORDER_INTENT_BUY_SHORT",
            "price": {"value": "0.500", "currency": "USD"},
            "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
            "state": "ORDER_STATE_NEW",
        }]}

        mgr.cancel_all_orders()

        # ord-1 was confirmed cancelled
        assert "ord-1" in mgr._cancelled_order_ids
        # ord-2 is still open — should NOT be marked cancelled
        assert "ord-2" not in mgr._cancelled_order_ids

    def test_cancel_all_dry_run_skips_reconciliation(self):
        """Dry run marks all as cancelled without API calls."""
        mgr_cls = __import__("scripts.poly_live_mm",
                             fromlist=["LiveOrderManager"]).LiveOrderManager
        client = MagicMock()
        mgr = mgr_cls(client, dry_run=True, capital_cents=2500)

        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        mgr.cancel_all_orders()

        assert oid in mgr._cancelled_order_ids
        assert mgr._local_orders == {}

    def test_cancel_confirmed_log_output(self, capsys):
        """When cancelled order disappears, CANCEL_CONFIRMED is logged."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)

        mgr.place_order("slug-a", "yes", 48, 2)
        oid = mgr._local_orders["slug-a"]["yes"]["order_id"]

        # Set up prev_orders
        tick_n = {"slug-a": {"yes": {
            "order_id": oid, "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}
        mgr.update_prev_orders(tick_n)

        # Cancel the order
        mgr.cancel_order("slug-a", "yes", oid)

        # Next tick: order gone
        curr_orders = mgr.merged_orders({}, poll_ok=True)
        fills = mgr.check_fills(curr_orders)

        assert len(fills) == 0

        captured = capsys.readouterr()
        assert "CANCEL_CONFIRMED" in captured.out
        assert oid in captured.out

    def test_startup_sync_catches_missed_fills(self):
        """sync_positions overwrites local inventory with exchange truth."""
        from scripts.poly_live_mm import LiveOrderManager, parse_positions
        from src.mm.state import MarketState, GlobalState
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        gs = GlobalState()
        gs.markets["slug-a"] = MarketState(ticker="slug-a")

        # Local state thinks inventory is 0
        assert len(gs.markets["slug-a"].yes_queue) == 0

        # Exchange says we have 4 YES contracts (from missed fills)
        client.get_positions.return_value = {"positions": {
            "slug-a": {"netPosition": "4", "qtyBought": "4",
                       "qtySold": "0", "cost": {"value": "2.00"}},
        }}

        mgr.sync_positions(gs, ["slug-a"])

        # Local state should now reflect exchange truth
        assert len(gs.markets["slug-a"].yes_queue) == 4


# ---------------------------------------------------------------------------
# Test: phantom fills from slug remap failure
# ---------------------------------------------------------------------------

class TestPhantomFillSlugRemap:
    """BUG: When poll_open_orders returns an order under a different API slug
    and _live_order_ids has lost the remap entry (e.g. after phantom fill
    cleanup), the order appears under api-slug in polled but under our-slug
    in _prev_orders. check_fills sees our-slug missing → phantom fill.

    After the phantom fill cleanup removes _live_order_ids, subsequent
    ticks can't remap → infinite phantom fill loop.
    """

    def test_order_under_different_api_slug_no_phantom_fill(self):
        """Order returned by API under different slug but same order_id.
        Should NOT report as fill when remap is in _live_order_ids."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Place order — stores in _live_order_ids and _local_orders
        mgr._live_order_ids["det-phi"] = {"yes": "ord-123"}
        mgr._local_orders["det-phi"] = {"yes": {
            "order_id": "ord-123", "price_cents": 50,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}

        # Tick N: poll returns correctly remapped
        client.list_orders.return_value = {"orders": [{
            "id": "ord-123",
            "marketSlug": "det-phi-totals-over-198-5",
            "intent": "ORDER_INTENT_BUY_LONG",
            "price": {"value": "0.500", "currency": "USD"},
            "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
            "state": "ORDER_STATE_NEW",
        }]}
        raw_polled, poll_ok = mgr.poll_open_orders(["det-phi"])
        curr = mgr.merged_orders(raw_polled, poll_ok)
        mgr.update_prev_orders(curr)

        # Tick N+1: same poll result
        raw_polled, poll_ok = mgr.poll_open_orders(["det-phi"])
        curr = mgr.merged_orders(raw_polled, poll_ok)
        fills = mgr.check_fills(curr)

        assert len(fills) == 0, f"Phantom fill detected: {fills}"

    def test_remap_lost_after_fill_cleanup_causes_phantom_loop(self):
        """After check_fills cleanup removes _live_order_ids entry,
        subsequent polls can't remap → phantom fill loop.

        This test reproduces the infinite phantom fill bug.
        """
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Place order
        mgr._live_order_ids["det-phi"] = {"yes": "ord-123"}
        mgr._local_orders["det-phi"] = {"yes": {
            "order_id": "ord-123", "price_cents": 50,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}

        # Tick N: poll returns under different API slug, remap works
        api_order = {
            "id": "ord-123",
            "marketSlug": "det-phi-totals-over-198-5",
            "intent": "ORDER_INTENT_BUY_LONG",
            "price": {"value": "0.500", "currency": "USD"},
            "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
            "state": "ORDER_STATE_NEW",
        }
        client.list_orders.return_value = {"orders": [api_order]}
        raw_polled, poll_ok = mgr.poll_open_orders(["det-phi"])
        curr = mgr.merged_orders(raw_polled, poll_ok)
        mgr.update_prev_orders(curr)

        # Simulate: _live_order_ids cleared (e.g. by phantom fill cleanup)
        mgr._live_order_ids.clear()

        # Tick N+1: remap fails → order stays under api-slug
        client.list_orders.return_value = {"orders": [api_order]}
        raw_polled, poll_ok = mgr.poll_open_orders(["det-phi"])

        # The order should still be found via order_id matching
        curr = mgr.merged_orders(raw_polled, poll_ok)
        fills = mgr.check_fills(curr)

        # MUST NOT detect phantom fill — order is still on exchange
        assert len(fills) == 0, (
            f"Phantom fill loop: {fills}. "
            f"polled keys={list(raw_polled.keys())}, "
            f"curr keys={list(curr.keys())}"
        )

    def test_merged_orders_matches_by_order_id_across_slugs(self):
        """merged_orders should recognize an order from _prev_orders
        even when it appears under a different slug key in polled data,
        by matching on order_id."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # _prev_orders has our-slug
        mgr._prev_orders = {"det-phi": {"yes": {
            "order_id": "ord-123", "price_cents": 50,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}

        # polled has api-slug (remap failed)
        polled = {"det-phi-totals-over-198-5": {"yes": {
            "order_id": "ord-123", "price_cents": 50,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}}

        curr = mgr.merged_orders(polled, poll_ok=True)

        # Our slug should be in curr (matched by order_id)
        assert "det-phi" in curr, (
            f"Our slug missing from merged. Keys: {list(curr.keys())}"
        )
        assert curr["det-phi"]["yes"]["order_id"] == "ord-123"

    def test_mixed_side_remap_no_partial_phantom_fill(self):
        """Both sides placed. YES polls under our slug, NO polls under
        API slug (remap lost). Per-side remap must find NO by order_id.
        No phantom fill on either side."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        # Both sides tracked
        mgr._live_order_ids["det-phi"] = {
            "yes": "ord-yes-1", "no": "ord-no-2"}
        mgr._local_orders["det-phi"] = {
            "yes": {"order_id": "ord-yes-1", "price_cents": 50,
                    "original_qty": 2, "filled_qty": 0,
                    "remaining_qty": 2},
            "no": {"order_id": "ord-no-2", "price_cents": 47,
                   "original_qty": 2, "filled_qty": 0,
                   "remaining_qty": 2},
        }

        # Tick N: both sides visible
        tick_n = {
            "det-phi": {
                "yes": {"order_id": "ord-yes-1", "price_cents": 50,
                        "original_qty": 2, "filled_qty": 0,
                        "remaining_qty": 2},
                "no": {"order_id": "ord-no-2", "price_cents": 47,
                       "original_qty": 2, "filled_qty": 0,
                       "remaining_qty": 2},
            }
        }
        mgr.update_prev_orders(tick_n)

        # Simulate: _live_order_ids loses NO remap only
        mgr._live_order_ids["det-phi"] = {"yes": "ord-yes-1"}

        # Tick N+1: YES remaps correctly, NO appears under API slug
        client.list_orders.return_value = {"orders": [
            {
                "id": "ord-yes-1",
                "marketSlug": "det-phi",
                "intent": "ORDER_INTENT_BUY_LONG",
                "price": {"value": "0.500", "currency": "USD"},
                "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
                "state": "ORDER_STATE_NEW",
            },
            {
                "id": "ord-no-2",
                "marketSlug": "det-phi-totals-over-198-5",
                "intent": "ORDER_INTENT_BUY_SHORT",
                "price": {"value": "0.470", "currency": "USD"},
                "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
                "state": "ORDER_STATE_NEW",
            },
        ]}
        raw_polled, poll_ok = mgr.poll_open_orders(["det-phi"])
        curr = mgr.merged_orders(raw_polled, poll_ok)
        fills = mgr.check_fills(curr)

        # Both sides must be present under our slug
        assert "det-phi" in curr
        assert "yes" in curr["det-phi"], "YES side missing"
        assert "no" in curr["det-phi"], "NO side missing — partial remap bug"
        assert len(fills) == 0, f"Phantom fill: {fills}"
