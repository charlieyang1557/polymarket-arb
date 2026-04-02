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
# Test: fill detection via portfolio.activities()
# ---------------------------------------------------------------------------

class TestActivitiesFillDetection:
    """Fill detection via portfolio.activities() — exchange-confirmed data.

    Eliminates phantom fills from order disappearance/slug remap issues.
    Uses trade IDs to avoid double-counting.
    """

    def _make_manager(self):
        from scripts.poly_live_mm import LiveOrderManager
        client = MagicMock()
        mgr = LiveOrderManager(client, dry_run=False, capital_cents=2500)
        return mgr, client

    def _trade_activity(self, trade_id: str, slug: str, price: str,
                        qty: int, is_aggressor: bool = False):
        return {
            "type": "ACTIVITY_TYPE_TRADE",
            "trade": {
                "id": trade_id,
                "marketSlug": slug,
                "state": "TRADE_STATE_FILLED",
                "createTime": "2026-04-02T12:00:00Z",
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
