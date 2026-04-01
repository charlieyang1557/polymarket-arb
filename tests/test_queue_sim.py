# tests/test_queue_sim.py
"""Tests for QueuePositionSimulator — realistic queue modeling."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Test: QueuePositionSimulator core mechanics
# ---------------------------------------------------------------------------

class TestQueuePositionSimulator:
    """Queue model: queue_ahead starts at total_depth, decreases with
    depth drops, fill triggers when queue_ahead reaches 0."""

    def _make_sim(self):
        from scripts.poly_paper_mm import QueuePositionSimulator
        return QueuePositionSimulator()

    def test_place_order_sets_queue_ahead(self):
        """Placing an order sets queue_ahead = current depth at price."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        order = sim.get_order("slug-a", "yes")
        assert order is not None
        assert order["queue_ahead"] == 500
        assert order["price"] == 55
        assert order["size"] == 2

    def test_depth_unchanged_no_fill(self):
        """Depth stays same → queue_ahead unchanged, no fill."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=500)
        assert fills == []
        order = sim.get_order("slug-a", "yes")
        assert order["queue_ahead"] == 500

    def test_depth_drops_100_queue_decreases(self):
        """Depth drops by 100 → queue_ahead drops by 100."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        # First tick sets baseline
        sim.update_tick("slug-a", "yes", price=55, current_depth=500)
        # Second tick: depth drops
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=400)
        assert fills == []
        order = sim.get_order("slug-a", "yes")
        assert order["queue_ahead"] == 400

    def test_depth_drops_to_zero_triggers_fill(self):
        """Depth drops to 0 → queue_ahead = 0 → fill triggered."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=100)
        sim.update_tick("slug-a", "yes", price=55, current_depth=100)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=0)
        assert len(fills) == 1
        assert fills[0]["side"] == "yes"
        assert fills[0]["filled"] == 2
        assert fills[0]["price"] == 55

    def test_queue_ahead_never_below_zero(self):
        """Depth drops massively → queue_ahead stays at 0, not negative."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=50)
        sim.update_tick("slug-a", "yes", price=55, current_depth=50)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=0)
        assert len(fills) == 1
        order = sim.get_order("slug-a", "yes")
        # Order should be consumed (None or remaining=0)
        assert order is None or order.get("remaining", 0) == 0

    def test_requote_resets_queue_to_new_depth(self):
        """Requote at new price → queue_ahead = depth at NEW price (back of queue)."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        # Drain some queue
        sim.update_tick("slug-a", "yes", price=55, current_depth=500)
        sim.update_tick("slug-a", "yes", price=55, current_depth=300)
        order = sim.get_order("slug-a", "yes")
        assert order["queue_ahead"] == 300
        # Requote to new price — goes to back of queue
        sim.requote("slug-a", "yes", new_price=57, new_depth=800)
        order = sim.get_order("slug-a", "yes")
        assert order["queue_ahead"] == 800
        assert order["price"] == 57

    def test_depth_increase_queue_unchanged(self):
        """New makers join (depth increases) → queue_ahead unchanged
        (they're behind us in the FIFO queue)."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        sim.update_tick("slug-a", "yes", price=55, current_depth=500)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=600)
        assert fills == []
        order = sim.get_order("slug-a", "yes")
        assert order["queue_ahead"] == 500  # unchanged — new makers behind us

    def test_cancel_order_removes_it(self):
        """Cancelled order is gone."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        sim.cancel_order("slug-a", "yes")
        assert sim.get_order("slug-a", "yes") is None

    def test_multiple_orders_independent(self):
        """YES and NO orders tracked independently per slug."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        sim.place_order("slug-a", "no", price=48, size=3, depth_at_price=200)
        yes_order = sim.get_order("slug-a", "yes")
        no_order = sim.get_order("slug-a", "no")
        assert yes_order["queue_ahead"] == 500
        assert no_order["queue_ahead"] == 200

    def test_gradual_drain_then_fill(self):
        """Realistic scenario: gradual queue drain over multiple ticks."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=300)
        sim.update_tick("slug-a", "yes", price=55, current_depth=300)
        # Tick 2: depth drops to 200 (100 drained)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=200)
        assert fills == []
        assert sim.get_order("slug-a", "yes")["queue_ahead"] == 200
        # Tick 3: depth drops to 50 (150 drained)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=50)
        assert fills == []
        assert sim.get_order("slug-a", "yes")["queue_ahead"] == 50
        # Tick 4: depth drops to 0 (50 drained → fill)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=0)
        assert len(fills) == 1
        assert fills[0]["filled"] == 2

    def test_partial_fill_not_supported(self):
        """Queue sim fills entire order at once (simplified model)."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=5, depth_at_price=100)
        sim.update_tick("slug-a", "yes", price=55, current_depth=100)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=0)
        assert len(fills) == 1
        assert fills[0]["filled"] == 5  # all at once

    def test_fill_includes_wait_time(self):
        """Fill event should include time waited since placement."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=100)
        sim.update_tick("slug-a", "yes", price=55, current_depth=100)
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=0)
        assert len(fills) == 1
        assert "waited_seconds" in fills[0]
        assert fills[0]["waited_seconds"] >= 0

    def test_no_order_update_tick_no_crash(self):
        """update_tick on non-existent order → empty fills, no crash."""
        sim = self._make_sim()
        fills = sim.update_tick("slug-a", "yes", price=55, current_depth=500)
        assert fills == []

    def test_different_slugs_independent(self):
        """Orders on different slugs are fully independent."""
        sim = self._make_sim()
        sim.place_order("slug-a", "yes", price=55, size=2, depth_at_price=500)
        sim.place_order("slug-b", "yes", price=60, size=3, depth_at_price=100)
        # Drain slug-b only
        sim.update_tick("slug-b", "yes", price=60, current_depth=100)
        sim.update_tick("slug-b", "yes", price=60, current_depth=0)
        # slug-a unaffected
        assert sim.get_order("slug-a", "yes")["queue_ahead"] == 500
        assert sim.get_order("slug-b", "yes") is None  # filled
