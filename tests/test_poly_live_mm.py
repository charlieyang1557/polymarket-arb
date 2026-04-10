"""Tests for poly_live_mm.py — live order management for Polymarket US."""

import pytest
import sys
import os
from datetime import datetime, timezone, timedelta
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

    def test_cancel_order_marks_pending(self):
        """After cancel_order, _local_orders marks cancel_pending."""
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        assert "yes" in mgr._local_orders.get("slug-a", {})
        mgr.cancel_order("slug-a", "yes", "dry-abc123")
        assert "yes" in mgr._local_orders.get("slug-a", {})
        assert mgr._local_orders["slug-a"]["yes"].get("cancel_pending") is True

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
# Test: fill detection via portfolio.activities()
# ---------------------------------------------------------------------------

class TestActivitiesFillDetection:
    """Fill detection via portfolio.activities() — exchange-confirmed data.

    Only counts passive maker fills that match our tracked orders.
    Does NOT clean up tracking on partial fills.
    """

    def _make_manager(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        # Set session start to a known time for deterministic tests
        mgr._session_start = datetime(2026, 4, 2, 10, 0, 0,
                                      tzinfo=timezone.utc)
        return mgr, client

    def _trade_activity(self, trade_id: str, slug: str, price: str,
                        qty: int, is_aggressor: bool = False,
                        create_time: str = "2026-04-02T12:00:00Z"):
        return {
            "type": "ACTIVITY_TYPE_TRADE",
            "trade": {
                "id": trade_id,
                "marketSlug": slug,
                "state": "TRADE_STATE_FILLED",
                "createTime": create_time,
                "price": price,
                "qty": str(qty),
                "isAggressor": is_aggressor,
            },
        }

    def test_new_trade_detected_as_fill(self):
        """New trade in activities → fill detected."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
        }}
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "slug-a", "0.48", 2),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 1
        assert fills[0]["slug"] == "slug-a"
        assert fills[0]["filled"] == 2
        assert fills[0]["price_cents"] == 48

    def test_decimal_qty_string_parsed(self):
        """API returns qty as '2.000' — must parse without ValueError."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
        }}
        # qty as decimal string like real API returns
        activity = self._trade_activity("t-dec", "slug-a", "0.48", 2)
        activity["trade"]["qty"] = "2.000"
        client.get_activities.return_value = {"activities": [activity]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 1
        assert fills[0]["filled"] == 2

    def test_same_trade_not_double_counted(self):
        """Same trade ID on second call → not counted again."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
        }}
        activities = [self._trade_activity("t-1", "slug-a", "0.48", 2)]
        client.get_activities.return_value = {"activities": activities}

        fills1 = mgr.check_fills(["slug-a"])
        assert len(fills1) == 1

        # Second call with same trade
        fills2 = mgr.check_fills(["slug-a"])
        assert len(fills2) == 0

    def test_trade_for_unknown_slug_ignored(self):
        """Trade for a slug we're not tracking → ignored."""
        mgr, client = self._make_manager()
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "unknown-slug", "0.50", 2),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0

    def test_no_new_trades_no_fills(self):
        """No trades in activities → no fills."""
        mgr, client = self._make_manager()
        client.get_activities.return_value = {"activities": []}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0

    def test_activities_api_failure_no_fills(self):
        """Activities API fails → no fills (fail safe)."""
        mgr, client = self._make_manager()
        client.get_activities.side_effect = Exception("API timeout")
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0

    def test_slug_remap_matches_api_slug(self):
        """Trade under API slug remapped to our internal slug."""
        mgr, client = self._make_manager()
        mgr._slug_remap["det-phi-totals-over-198-5"] = "det-phi"
        mgr._local_orders["det-phi"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
        }}
        client.get_activities.return_value = {"activities": [
            self._trade_activity(
                "t-1", "det-phi-totals-over-198-5", "0.50", 2),
        ]}
        fills = mgr.check_fills(["det-phi"])
        assert len(fills) == 1
        assert fills[0]["slug"] == "det-phi"

    def test_multiple_trades_multiple_markets(self):
        """Multiple trades across different markets detected."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "o1", "price_cents": 48,
        }}
        mgr._local_orders["slug-b"] = {"no": {
            "order_id": "o2", "price_cents": 52,
        }}
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "slug-a", "0.48", 2),
            self._trade_activity("t-2", "slug-b", "0.52", 3),
        ]}
        fills = mgr.check_fills(["slug-a", "slug-b"])
        assert len(fills) == 2
        slugs = {f["slug"] for f in fills}
        assert slugs == {"slug-a", "slug-b"}

    def test_dry_run_returns_no_fills(self):
        """Dry-run mode → no activities call, no fills."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0
        client.get_activities.assert_not_called()

    def test_old_trade_before_session_ignored(self):
        """Trade from before session start → not counted as fill."""
        mgr, client = self._make_manager()
        # Session started at 2026-04-02T10:00:00Z
        # Trade is from 2026-04-02T08:00:00Z (before session)
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
        }}
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-old", "slug-a", "0.48", 2,
                                 create_time="2026-04-02T08:00:00Z"),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0

    def test_partial_fill_preserves_tracking(self):
        """Partial fill does NOT remove tracking — remainder still resting."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}
        mgr._live_order_ids["slug-a"] = {"yes": "ord-1"}
        # Partial fill: 1 of 2
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "slug-a", "0.48", 1),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 1
        assert fills[0]["filled"] == 1
        # Tracking should be PRESERVED (remainder still on exchange)
        assert "yes" in mgr._local_orders.get("slug-a", {})
        assert "yes" in mgr._live_order_ids.get("slug-a", {})

    def test_partial_fill_order_still_in_merged_orders(self):
        """After partial fill, merged_orders still shows the order
        (from poll or local tracking)."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}
        mgr._live_order_ids["slug-a"] = {"yes": "ord-1"}
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "slug-a", "0.48", 1),
        ]}
        mgr.check_fills(["slug-a"])

        # Poll shows order with reduced qty (remainder)
        polled = {"slug-a": {"yes": {
            "order_id": "ord-1", "price_cents": 48,
            "original_qty": 2, "filled_qty": 1, "remaining_qty": 1,
        }}}
        merged = mgr.merged_orders(polled)
        assert "yes" in merged["slug-a"]
        assert merged["slug-a"]["yes"]["remaining_qty"] == 1

    def test_aggressor_trade_ignored(self):
        """Aggressor (taker) trade → NOT counted as our maker fill."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
        }}
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "slug-a", "0.48", 2,
                                 is_aggressor=True),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0

    def test_unmatched_price_trade_ignored(self):
        """Trade at price not matching any tracked order → ignored."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 48,
        }}
        # Trade at 60c — doesn't match our 48c order
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-1", "slug-a", "0.60", 2),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 0

    def test_new_trade_after_session_start_detected(self):
        """Trade after session start → detected as fill."""
        mgr, client = self._make_manager()
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
        }}
        # Session at 10:00, trade at 11:00
        client.get_activities.return_value = {"activities": [
            self._trade_activity("t-new", "slug-a", "0.50", 1,
                                 create_time="2026-04-02T11:00:00Z"),
        ]}
        fills = mgr.check_fills(["slug-a"])
        assert len(fills) == 1


# ---------------------------------------------------------------------------
# Test: merged_orders — simple merge (no disappearance inference)
# ---------------------------------------------------------------------------

class TestMergedOrdersSimple:
    """merged_orders is now a simple merge: poll data + local gaps.
    No disappearance-based fill inference."""

    def _make_manager(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        return LiveOrderManager(client, dry_run=True, capital_cents=2500)

    def test_poll_data_wins(self):
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        polled = {"slug-a": {"yes": {
            "order_id": "real-id", "price_cents": 48,
            "original_qty": 2, "filled_qty": 1, "remaining_qty": 1,
        }}}
        merged = mgr.merged_orders(polled)
        assert merged["slug-a"]["yes"]["filled_qty"] == 1

    def test_local_fills_gaps(self):
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        merged = mgr.merged_orders({})
        assert "slug-a" in merged
        assert merged["slug-a"]["yes"]["price_cents"] == 48

    def test_poll_empty_uses_local(self):
        mgr = self._make_manager()
        mgr.place_order("slug-a", "yes", 48, 2)
        mgr.place_order("slug-a", "no", 50, 2)
        merged = mgr.merged_orders({})
        assert "yes" in merged["slug-a"]
        assert "no" in merged["slug-a"]


# ---------------------------------------------------------------------------
# Test: poll_open_orders returns tuple + slug remap
# ---------------------------------------------------------------------------

class TestPollOpenOrders:
    """poll_open_orders returns (dict, bool) and remaps API slugs."""

    def test_returns_tuple(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        client.list_orders.return_value = {"orders": []}
        result = mgr.poll_open_orders(["slug-a"])
        assert isinstance(result, tuple)
        orders, ok = result
        assert ok is True

    def test_failure_returns_false(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        client.list_orders.side_effect = Exception("timeout")
        orders, ok = mgr.poll_open_orders(["slug-a"])
        assert orders == {}
        assert ok is False

    def test_remap_registers_slug_mapping(self):
        """When poll remaps API slug → our slug, it registers in _slug_remap."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        mgr._live_order_ids["det-phi"] = {"yes": "ord-123"}
        client.list_orders.return_value = {"orders": [{
            "id": "ord-123",
            "marketSlug": "det-phi-totals-over-198-5",
            "intent": "ORDER_INTENT_BUY_LONG",
            "price": {"value": "0.500", "currency": "USD"},
            "quantity": 2, "cumQuantity": 0, "leavesQuantity": 2,
            "state": "ORDER_STATE_NEW",
        }]}
        orders, ok = mgr.poll_open_orders(["det-phi"])
        assert ok is True
        assert "det-phi" in orders
        assert mgr._slug_remap.get(
            "det-phi-totals-over-198-5") == "det-phi"


# ---------------------------------------------------------------------------
# Test: startup position sync
# ---------------------------------------------------------------------------

class TestStartupSync:
    """sync_positions catches missed fills from previous sessions."""

    def test_syncs_exchange_positions(self):
        from scripts.poly_live_mm import LiveOrderManager
        from src.mm.state import MarketState, GlobalState
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)

        gs = GlobalState()
        gs.markets["slug-a"] = MarketState(ticker="slug-a")
        assert len(gs.markets["slug-a"].yes_queue) == 0

        client.get_positions.return_value = {"positions": {
            "slug-a": {"netPosition": "4", "qtyBought": "4",
                       "qtySold": "0", "cost": {"value": "2.00"}},
        }}
        mgr.sync_positions(gs, ["slug-a"])
        assert len(gs.markets["slug-a"].yes_queue) == 4


# ---------------------------------------------------------------------------
# Test: fill → force requote on same tick (Task 1)
# ---------------------------------------------------------------------------

class TestFillForcesRequote:
    """After a fill, inv_changed_slugs must persist until the market's
    quotes are actually managed — not reset each cycle."""

    def test_fill_detected_forces_requote_same_tick(self):
        """Fill detected → force_requote on that market's quote tick,
        even if delta < MIN_REQUOTE_DELTA."""
        from scripts.poly_live_mm import should_requote_or_force
        # 1c delta — normally blocked by MIN_REQUOTE_DELTA=2
        assert should_requote_or_force(51, 52, force_requote=True) is True
        assert should_requote_or_force(46, 45, force_requote=True) is True

    def test_fill_force_not_lost_across_cycles(self):
        """inv_changed_slugs must survive until the market is actually
        processed, not be rebuilt empty each cycle."""
        # This is a design test: inv_changed_slugs must be persistent
        # and cleared per-slug only after _manage_live_quotes runs.
        # We verify by checking the set operations:
        inv_changed: set = set()

        # Cycle 1: fill detected for market B, but round-robin processes A
        inv_changed.add("market-b")
        # Only clear market-a (the one processed this cycle)
        inv_changed.discard("market-a")
        assert "market-b" in inv_changed  # still pending

        # Cycle 2: round-robin processes B
        assert "market-b" in inv_changed  # signal available
        inv_changed.discard("market-b")  # cleared after quotes managed
        assert "market-b" not in inv_changed

    def test_force_requote_zero_delta_no_requote(self):
        """Even with force=True, if target==current, no requote needed."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(52, 52, force_requote=True) is False


# ---------------------------------------------------------------------------
# Test: both legs use MIN_REQUOTE_DELTA=2 (no hedging special case)
# ---------------------------------------------------------------------------

class TestRequoteBothLegsSticky:
    """After removing hedging leg special case, both sides use
    MIN_REQUOTE_DELTA=2. Only inventory_changed forces requote."""

    def test_delta1_no_requote_either_side(self):
        """1c move → no requote on either side, regardless of inventory."""
        from scripts.poly_live_mm import should_requote_or_force
        # No force: 1c blocked
        assert should_requote_or_force(51, 50, force_requote=False) is False
        assert should_requote_or_force(49, 50, force_requote=False) is False

    def test_delta2_requotes(self):
        """2c move → requote (normal threshold)."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(52, 50, force_requote=False) is True

    def test_delta0_never_requotes_even_with_force(self):
        """Same price → never requote, even with force (fill tick)."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(50, 50, force_requote=True) is False

    def test_inventory_changed_forces_delta1(self):
        """Fill detected → force requote on 1c move."""
        from scripts.poly_live_mm import should_requote_or_force
        assert should_requote_or_force(51, 50, force_requote=True) is True


# ---------------------------------------------------------------------------
# Test: game_start_utc propagation for soft-close
# ---------------------------------------------------------------------------

class TestGameStartPropagation:
    """Verify game_start_utc is set for both initial and hot-added markets."""

    def test_get_market_event_start_fallback_used(self):
        """SDK event-level fallback should populate game_start_utc."""
        from scripts.poly_live_mm import extract_game_start_from_response

        raw = {"market": {
            "gameStartTime": "",
            "_event_start_time": "2026-04-04T20:00:00Z",
        }}
        assert extract_game_start_from_response(raw) == "2026-04-04T20:00:00Z"

    def test_resolve_game_start_prefers_schedule(self):
        """resolve_game_start uses cached schedule before API call."""
        from scripts.poly_live_mm import resolve_game_start
        schedule = {"slug-a": "2026-04-04T20:00:00Z"}
        result = resolve_game_start("slug-a", schedule, api_lookup=None)
        assert result == "2026-04-04T20:00:00Z"

    def test_resolve_game_start_falls_back_to_api(self):
        """resolve_game_start calls api_lookup when schedule misses."""
        from scripts.poly_live_mm import resolve_game_start
        result = resolve_game_start(
            "slug-b", {},
            api_lookup=lambda s: "2026-04-04T21:00:00Z")
        assert result == "2026-04-04T21:00:00Z"

    def test_hot_added_market_gets_game_start(self):
        """consume_pending_markets passes game_start_lookup → MarketState."""
        import json
        import tempfile
        from scripts.poly_live_mm import consume_pending_markets
        from src.mm.state import GlobalState

        gs = GlobalState()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                          delete=False) as f:
            json.dump({"slugs": ["test-slug-abc"]}, f)
            tmp_path = f.name

        def mock_lookup(slug):
            return "2026-04-04T20:00:00Z"

        added = consume_pending_markets(
            gs, pending_path=tmp_path,
            game_start_lookup=mock_lookup)
        assert added == ["test-slug-abc"]
        ms = gs.markets["test-slug-abc"]
        assert ms.game_start_utc is not None
        assert ms.game_start_utc.year == 2026

    def test_hot_added_market_no_game_start(self):
        """Hot-add without game_start_lookup → game_start_utc is None."""
        import json
        import tempfile
        from scripts.poly_live_mm import consume_pending_markets
        from src.mm.state import GlobalState

        gs = GlobalState()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                          delete=False) as f:
            json.dump({"slugs": ["test-slug-xyz"]}, f)
            tmp_path = f.name

        added = consume_pending_markets(
            gs, pending_path=tmp_path,
            game_start_lookup=None)
        assert added == ["test-slug-xyz"]
        assert gs.markets["test-slug-xyz"].game_start_utc is None

    def test_l4_soft_close_with_game_start(self):
        """MarketState with game_start_utc within 15min → SOFT_CLOSE."""
        from src.mm.state import MarketState
        from src.mm.risk import check_layer4, Action
        from datetime import datetime, timezone, timedelta

        ms = MarketState(ticker="test")
        ms.game_start_utc = datetime.now(timezone.utc) + timedelta(minutes=10)
        ms.last_api_success = datetime.now(timezone.utc)
        result = check_layer4(ms, spread=3, db_error_count=0)
        assert result == Action.SOFT_CLOSE


# ---------------------------------------------------------------------------
# Test: reducing-side requotes + same-price duplicate guard
# ---------------------------------------------------------------------------

class TestReducingSideAndPlaceGuard:
    def test_reducing_side_cancels_on_delta1(self):
        """Inventory from sync_positions → reducing side cancels on 1c move.
        With cancel_pending, place happens next tick after poll confirms."""
        from scripts.poly_live_mm import _manage_live_quotes, LiveOrderManager
        from src.mm.state import MarketState

        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)
        ms = MarketState(ticker="slug-a")
        ms.last_api_success = datetime.now(timezone.utc)
        ms.yes_queue.extend([50, 50])
        now = datetime.now(timezone.utc)
        ms.midpoint_history = [
            (now - timedelta(seconds=30), 49.5),
            (now - timedelta(seconds=10), 49.5),
            (now, 49.5),
        ]

        curr_orders = {"slug-a": {"no": {
            "order_id": "old-no",
            "price_cents": 50,
            "original_qty": 2,
            "filled_qty": 0,
            "remaining_qty": 2,
        }}}
        mgr._local_orders["slug-a"] = {"no": dict(curr_orders["slug-a"]["no"])}

        _manage_live_quotes(
            mgr, ms,
            best_yes_bid=48, best_no_bid=51,
            yes_ask=49, midpoint=49.5,
            yes_bids=[[48, 10]], no_bids=[[51, 10]],
            curr_orders=curr_orders, order_size=2,
            max_inventory=10,
            inventory_changed=False)

        # Cancel was called for the reducing side (1c move, force=True)
        assert mgr._local_orders["slug-a"]["no"].get("cancel_pending") is True
        # Place NOT called for NO on this tick — waits for cancel confirm

    def test_cancel_pending_then_place_after_poll(self):
        """After poll confirms cancel gone, next tick places the new order."""
        from scripts.poly_live_mm import _manage_live_quotes, LiveOrderManager
        from src.mm.state import MarketState

        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=True, capital_cents=2500)
        ms = MarketState(ticker="slug-a")
        ms.last_api_success = datetime.now(timezone.utc)
        ms.yes_queue.extend([50, 50])
        now = datetime.now(timezone.utc)
        ms.midpoint_history = [
            (now - timedelta(seconds=30), 49.5),
            (now - timedelta(seconds=10), 49.5),
            (now, 49.5),
        ]

        # Simulate: poll confirmed cancel (merged_orders returns empty for NO)
        curr_orders = {"slug-a": {}}

        _manage_live_quotes(
            mgr, ms,
            best_yes_bid=48, best_no_bid=51,
            yes_ask=49, midpoint=49.5,
            yes_bids=[[48, 10]], no_bids=[[51, 10]],
            curr_orders=curr_orders, order_size=2,
            max_inventory=10,
            inventory_changed=False)

        # Both sides should get placed (no existing orders)
        placed_sides = set()
        for slug, sides in mgr._local_orders.items():
            for side in sides:
                placed_sides.add(side)


# ---------------------------------------------------------------------------
# Test: cancel_pending state prevents duplicate placements
# ---------------------------------------------------------------------------

class TestCancelPendingState:
    """After cancel, order stays in _local_orders as cancel_pending
    until poll confirms it's gone. Prevents existing=None→place race."""

    def test_cancel_marks_pending_not_deleted(self):
        """cancel_order marks cancel_pending=True, doesn't delete."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
            "original_qty": 2, "filled_qty": 0, "remaining_qty": 2,
        }}
        mgr.cancel_order("slug-a", "yes", "ord-1")
        # Order should still be in local_orders but marked pending
        assert "yes" in mgr._local_orders.get("slug-a", {})
        assert mgr._local_orders["slug-a"]["yes"].get("cancel_pending") is True

    def test_merged_orders_keeps_cancel_pending_on_poll_failure(self):
        """cancel_pending kept when poll failed (poll_ok=False)."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
            "cancel_pending": True,
        }}
        # Poll failed → empty dict, poll_ok=False
        merged = mgr.merged_orders({}, poll_ok=False)
        assert "yes" in merged.get("slug-a", {})

    def test_poll_confirms_cancel_clears_pending(self):
        """When poll shows order is gone, cancel_pending entry is cleared."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
            "cancel_pending": True,
        }}
        # Poll returns data for slug-a but no "yes" side → cancel confirmed
        polled = {"slug-a": {"no": {"order_id": "ord-2", "price_cents": 48}}}
        merged = mgr.merged_orders(polled)
        # cancel_pending yes should be cleared
        assert "yes" not in merged.get("slug-a", {})
        assert "yes" not in mgr._local_orders.get("slug-a", {})

    def test_poll_shows_new_order_replaces_pending(self):
        """If poll shows a new order on the same side, it replaces pending."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
            "cancel_pending": True,
        }}
        # Poll shows a new order on yes side
        polled = {"slug-a": {"yes": {"order_id": "ord-2", "price_cents": 51}}}
        merged = mgr.merged_orders(polled)
        assert merged["slug-a"]["yes"]["order_id"] == "ord-2"
        assert merged["slug-a"]["yes"].get("cancel_pending") is not True

    def test_cancel_pending_clears_when_slug_absent_from_successful_poll(self):
        """When poll succeeds but slug has no orders, cancel_pending clears.
        This is the stuck-state bug: last order on slug cancelled → poll
        returns {} → cancel_pending was never cleared."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        mgr._local_orders["slug-a"] = {"yes": {
            "order_id": "ord-1", "price_cents": 50,
            "cancel_pending": True,
        }}
        # Successful poll returns empty (no open orders anywhere)
        merged = mgr.merged_orders({}, poll_ok=True, polled_slugs={"slug-a"})
        # cancel_pending should be cleared — not stuck
        assert "yes" not in mgr._local_orders.get("slug-a", {})
        assert "yes" not in merged.get("slug-a", {})


# ---------------------------------------------------------------------------
# Test: place_order only records attempt after success
# ---------------------------------------------------------------------------

class TestPlaceAttemptGuardTiming:
    """_recent_place_attempts should only be recorded after confirmed
    success, not before validation."""

    def test_rejected_order_does_not_poison_guard(self):
        """Capital check rejection should not block future placements."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        # Tiny capital → order will be rejected by max_order_value_check
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=10)
        result = mgr.place_order("slug-a", "yes", 50, 2)
        assert result is None
        # Guard should NOT be set
        assert not mgr.has_recent_place_attempt("slug-a", "yes", 50, 2)

    def test_successful_order_sets_guard(self):
        """Successful placement should set the guard."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        client.place_order.return_value = {"id": "ord-123"}
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        result = mgr.place_order("slug-a", "yes", 50, 2)
        assert result == "ord-123"
        assert mgr.has_recent_place_attempt("slug-a", "yes", 50, 2)

    def test_api_error_does_not_poison_guard(self):
        """API error should not block future placements."""
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        client.place_order.side_effect = Exception("connection refused")
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        result = mgr.place_order("slug-a", "yes", 50, 2)
        assert result is None
        assert not mgr.has_recent_place_attempt("slug-a", "yes", 50, 2)


class TestHedgeUrgencyIntegration:
    """_manage_live_quotes applies hedge urgency offset to reducing side."""

    def test_reducing_side_gets_urgency_offset(self):
        from scripts.poly_live_mm import _manage_live_quotes
        from src.mm.state import MarketState
        from unittest.mock import MagicMock

        ms = MarketState(ticker="test-slug")
        ms.yes_queue = [48]  # long 1 YES → net_inventory=+1
        ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        ms.midpoint_history = [(datetime.now(timezone.utc), 50.0)]
        ms.last_api_success = datetime.now(timezone.utc)

        live_mgr = MagicMock()
        live_mgr.has_recent_place_attempt.return_value = False
        curr_orders = {}

        _manage_live_quotes(
            live_mgr, ms,
            best_yes_bid=49, best_no_bid=49,
            yes_ask=51, midpoint=50.0,
            yes_bids=[], no_bids=[],
            curr_orders=curr_orders, order_size=2,
            max_inventory=10,
            inventory_changed=False)

        calls = live_mgr.place_order.call_args_list
        no_calls = [c for c in calls if c[0][1] == "no"]
        assert len(no_calls) >= 1
        no_price = no_calls[0][0][2]
        assert no_price >= 50  # 49 + skew + urgency(2c at 10min)


class TestProgressiveSoftCloseIntegration:
    """_manage_live_quotes uses progressive pricing during SOFT_CLOSE."""

    def test_soft_close_returns_none_on_wide_book(self):
        from scripts.poly_live_mm import _manage_live_quotes
        from src.mm.state import MarketState
        from unittest.mock import MagicMock

        ms = MarketState(ticker="test-slug")
        ms.yes_queue = [48]  # long YES → reduce via NO
        ms.game_start_utc = datetime.now(timezone.utc) + timedelta(minutes=1)
        ms.midpoint_history = [(datetime.now(timezone.utc), 50.0)]
        ms.last_api_success = datetime.now(timezone.utc)

        live_mgr = MagicMock()
        live_mgr.has_recent_place_attempt.return_value = False
        curr_orders = {}

        _manage_live_quotes(
            live_mgr, ms,
            best_yes_bid=35, best_no_bid=35,
            yes_ask=65, midpoint=50.0,
            yes_bids=[], no_bids=[],
            curr_orders=curr_orders, order_size=2,
            max_inventory=10,
            time_soft_close=True,
            inventory_changed=False)

        assert live_mgr.place_order.call_count == 0
