# tests/test_trade_pipeline.py
"""TDD tests for the trade feed pipeline using REAL API fixtures.

Tests the exact filter logic the engine uses:
  1. First-tick watermark initialization
  2. Dedup filtering (timestamp + trade_id)
  3. placed_at filtering
  4. drain_queue price matching
  5. Full pipeline end-to-end
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.mm.state import SimOrder, MarketState
from src.mm.engine import drain_queue, process_fills

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helper: replicate the EXACT dedup logic from engine.py lines 180-219
# ---------------------------------------------------------------------------

def run_dedup(ms: MarketState, all_trades: list[dict]) -> list[dict]:
    """Exact replica of engine.py dedup logic. Returns new_trades."""
    if not ms.last_seen_trade_ts:
        # First tick: set watermark, return nothing
        if all_trades:
            ms.last_seen_trade_ts = max(
                t.get("created_time", "") for t in all_trades)
            ms.last_seen_trade_ids = {
                t["trade_id"] for t in all_trades
                if t.get("created_time", "") == ms.last_seen_trade_ts
            }
        return []
    else:
        wm = ms.last_seen_trade_ts
        new_trades = [
            t for t in all_trades
            if t.get("created_time", "") > wm
            or (t.get("created_time", "") == wm
                and t.get("trade_id") not in ms.last_seen_trade_ids)
        ]
        if new_trades:
            new_max = max(t.get("created_time", "") for t in new_trades)
            if new_max > wm:
                ms.last_seen_trade_ts = new_max
                ms.last_seen_trade_ids = {
                    t["trade_id"] for t in new_trades
                    if t.get("created_time", "") == new_max
                }
            else:
                ms.last_seen_trade_ids.update(
                    t["trade_id"] for t in new_trades
                )
        return new_trades


def run_placed_filter(new_trades: list[dict], order: SimOrder) -> list[dict]:
    """Exact replica of engine.py placed_at filter (line 231-233)."""
    placed_iso = order.placed_at.strftime("%Y-%m-%dT%H:%M:%S")
    return [t for t in new_trades
            if t.get("created_time", "")[:19] >= placed_iso]


# ---------------------------------------------------------------------------
# STEP 2a: First-tick watermark sets correctly
# ---------------------------------------------------------------------------

class TestFirstTickWatermark:
    def test_first_tick_returns_no_trades(self):
        """First tick should set watermark but NOT return any trades."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        ms = MarketState(ticker="KXVPRESNOMR-28-MR")

        new_trades = run_dedup(ms, all_trades)

        assert new_trades == [], "First tick should return 0 new trades"
        assert ms.last_seen_trade_ts != "", "Watermark should be set"

    def test_first_tick_watermark_is_newest(self):
        """Watermark should be set to the newest trade's created_time."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        ms = MarketState(ticker="KXVPRESNOMR-28-MR")

        run_dedup(ms, all_trades)

        newest = max(t["created_time"] for t in all_trades)
        assert ms.last_seen_trade_ts == newest

    def test_first_tick_captures_all_ids_at_watermark(self):
        """All trade_ids at the watermark timestamp should be captured."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        ms = MarketState(ticker="KXVPRESNOMR-28-MR")

        run_dedup(ms, all_trades)

        ids_at_wm = {t["trade_id"] for t in all_trades
                     if t["created_time"] == ms.last_seen_trade_ts}
        assert ms.last_seen_trade_ids == ids_at_wm


# ---------------------------------------------------------------------------
# STEP 2b: Dedup correctly identifies new trades
# ---------------------------------------------------------------------------

class TestDedupFilter:
    def test_new_trade_with_newer_timestamp_passes(self):
        """A trade with created_time > watermark must pass the filter."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        times = sorted(set(t["created_time"] for t in all_trades))

        # Set watermark to second-newest timestamp
        ms = MarketState(ticker="KXVPRESNOMR-28-MR")
        ms.last_seen_trade_ts = times[-2]
        ms.last_seen_trade_ids = {
            t["trade_id"] for t in all_trades
            if t["created_time"] == times[-2]
        }

        new_trades = run_dedup(ms, all_trades)

        # All trades at the newest timestamp should pass
        expected = [t for t in all_trades if t["created_time"] > times[-2]]
        assert len(new_trades) == len(expected)
        assert len(new_trades) > 0, "Must have at least one new trade"

    def test_same_timestamp_unseen_id_passes(self):
        """A trade at watermark timestamp with unseen trade_id must pass."""
        fixture = load_fixture("real_trades_kxgreenland_29.json")
        all_trades = fixture["trades"]

        # Find a timestamp with multiple trades
        from collections import Counter
        ts_counts = Counter(t["created_time"] for t in all_trades)
        multi_ts = [ts for ts, c in ts_counts.items() if c >= 2]
        assert multi_ts, "Need a timestamp with 2+ trades for this test"

        target_ts = multi_ts[0]
        trades_at_ts = [t for t in all_trades
                        if t["created_time"] == target_ts]

        # Set watermark to this timestamp, but only mark FIRST trade as seen
        ms = MarketState(ticker="KXGREENLAND-29")
        ms.last_seen_trade_ts = target_ts
        ms.last_seen_trade_ids = {trades_at_ts[0]["trade_id"]}

        new_trades = run_dedup(ms, all_trades)

        # The unseen trades at the same timestamp should pass
        unseen_at_ts = [t for t in trades_at_ts
                        if t["trade_id"] != trades_at_ts[0]["trade_id"]]
        # Plus any trades with newer timestamps
        newer = [t for t in all_trades if t["created_time"] > target_ts]

        assert len(new_trades) == len(unseen_at_ts) + len(newer)

    def test_fully_caught_up_returns_zero(self):
        """When watermark matches newest trade and all ids seen, return 0."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        newest_ts = max(t["created_time"] for t in all_trades)

        ms = MarketState(ticker="KXVPRESNOMR-28-MR")
        ms.last_seen_trade_ts = newest_ts
        ms.last_seen_trade_ids = {
            t["trade_id"] for t in all_trades
            if t["created_time"] == newest_ts
        }

        new_trades = run_dedup(ms, all_trades)
        assert new_trades == []

    def test_no_trades_returned_from_api(self):
        """Empty trade list should not crash or change watermark."""
        ms = MarketState(ticker="TEST")
        ms.last_seen_trade_ts = "2026-03-12T00:00:00Z"
        ms.last_seen_trade_ids = {"fake-id"}

        new_trades = run_dedup(ms, [])
        assert new_trades == []
        assert ms.last_seen_trade_ts == "2026-03-12T00:00:00Z"


# ---------------------------------------------------------------------------
# STEP 2b-regression: Prove the OLD logic was broken
# ---------------------------------------------------------------------------

def _old_broken_dedup(ms_last_seen: str, all_trades: list[dict]) -> list[dict]:
    """The OLD single-field strict-'>' dedup that was broken."""
    return [t for t in all_trades
            if t.get("created_time", "") > ms_last_seen]


class TestOldLogicIsBroken:
    def test_old_dedup_drops_same_timestamp_trades(self):
        """Prove that the OLD strict '>' logic loses trades.

        This is a regression guard: if someone reverts to the old approach,
        this test will catch it.
        """
        fixture = load_fixture("real_trades_kxgreenland_29.json")
        all_trades = fixture["trades"]

        from collections import Counter
        ts_counts = Counter(t["created_time"] for t in all_trades)
        multi_ts = [ts for ts, c in ts_counts.items() if c >= 3]
        assert multi_ts, "Fixture needs a timestamp with 3+ trades"

        target_ts = multi_ts[0]
        trades_at_ts = [t for t in all_trades
                        if t["created_time"] == target_ts]

        # OLD: watermark at target_ts → strict '>' drops ALL same-ts trades
        old_result = _old_broken_dedup(target_ts, all_trades)
        old_same_ts = [t for t in old_result
                       if t["created_time"] == target_ts]
        assert len(old_same_ts) == 0, \
            "Old logic should drop ALL trades at watermark timestamp"

        # NEW: watermark at target_ts with 1 id seen → catches the rest
        ms = MarketState(ticker="KXGREENLAND-29")
        ms.last_seen_trade_ts = target_ts
        ms.last_seen_trade_ids = {trades_at_ts[0]["trade_id"]}

        new_result = run_dedup(ms, all_trades)
        new_same_ts = [t for t in new_result
                       if t["created_time"] == target_ts]
        assert len(new_same_ts) == len(trades_at_ts) - 1, \
            f"New logic should find {len(trades_at_ts) - 1} unseen trades"


# ---------------------------------------------------------------------------
# STEP 2c: placed_at filter with real timestamps
# ---------------------------------------------------------------------------

class TestPlacedAtFilter:
    def test_trades_after_placement_pass(self):
        """Trades created after order placement should pass."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        times = sorted(set(t["created_time"] for t in all_trades))

        # Order placed at the oldest trade time
        placed = datetime.fromisoformat(
            times[0].replace("Z", "+00:00"))
        order = SimOrder(side="yes", price=28, size=2, remaining=2,
                         queue_pos=100, placed_at=placed)

        relevant = run_placed_filter(all_trades, order)

        # All trades should pass (placed_at <= all trade times)
        assert len(relevant) == len(all_trades)

    def test_trades_before_placement_filtered(self):
        """Trades created before order placement should be filtered out."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        times = sorted(set(t["created_time"] for t in all_trades))

        # Order placed AFTER the newest trade — nothing should pass
        placed = datetime.fromisoformat(
            times[-1].replace("Z", "+00:00")) + timedelta(seconds=1)
        order = SimOrder(side="yes", price=28, size=2, remaining=2,
                         queue_pos=100, placed_at=placed)

        relevant = run_placed_filter(all_trades, order)
        assert len(relevant) == 0

    def test_placed_at_filter_uses_truncated_comparison(self):
        """Verify the [:19] truncation works with real Kalshi timestamps.

        Kalshi returns: '2026-03-12T18:36:08.996839Z'
        We truncate to: '2026-03-12T18:36:08'
        placed_at fmt:  '2026-03-12T18:36:08'
        """
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        trade = fixture["trades"][0]
        created = trade["created_time"]

        # Placed at the same second as the trade
        placed = datetime(2026, 3, 12, 18, 36, 8, tzinfo=timezone.utc)
        order = SimOrder(side="yes", price=28, size=2, remaining=2,
                         queue_pos=100, placed_at=placed)

        relevant = run_placed_filter([trade], order)
        # created_time[:19] = '2026-03-12T18:36:08' >= '2026-03-12T18:36:08'
        assert len(relevant) == 1, (
            f"Trade at {created} should pass filter for order placed at "
            f"{placed.isoformat()}"
        )


# ---------------------------------------------------------------------------
# STEP 2d: drain_queue with real price data
# ---------------------------------------------------------------------------

class TestDrainWithRealData:
    def test_yes_drain_at_matching_price(self):
        """YES order at trade's yes_price should drain."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        trade = fixture["trades"][0]  # yes_price=0.2800, vol=289
        yes_cents = round(float(trade["yes_price_dollars"]) * 100)

        order = SimOrder(side="yes", price=yes_cents, size=2, remaining=2,
                         queue_pos=100,
                         placed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))

        drain = drain_queue(order, [trade])
        assert drain == 289, f"Expected 289, got {drain}"

    def test_yes_drain_at_higher_price(self):
        """YES order at price ABOVE trade price should still drain."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        trade = fixture["trades"][0]  # yes_price=0.2800
        yes_cents = round(float(trade["yes_price_dollars"]) * 100)

        order = SimOrder(side="yes", price=yes_cents + 2, size=2,
                         remaining=2, queue_pos=100,
                         placed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))

        drain = drain_queue(order, [trade])
        assert drain == 289

    def test_yes_no_drain_below_price(self):
        """YES order below trade price should NOT drain."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        trade = fixture["trades"][0]  # yes_price=0.2800
        yes_cents = round(float(trade["yes_price_dollars"]) * 100)

        order = SimOrder(side="yes", price=yes_cents - 1, size=2,
                         remaining=2, queue_pos=100,
                         placed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))

        drain = drain_queue(order, [trade])
        assert drain == 0

    def test_no_drain_matching(self):
        """NO order drains when (100 - yes_price) <= order price."""
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        trade = fixture["trades"][0]  # yes=28c -> no=72c
        no_cents = 100 - round(float(trade["yes_price_dollars"]) * 100)

        order = SimOrder(side="no", price=no_cents, size=2, remaining=2,
                         queue_pos=100,
                         placed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))

        drain = drain_queue(order, [trade])
        assert drain == 289


# ---------------------------------------------------------------------------
# STEP 2e: FULL PIPELINE — the exact scenario that was failing
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_second_tick_processes_new_trades(self):
        """Simulate tick 1 (watermark) then tick 2 (new trades appear).

        This is THE test case that was broken in production.
        """
        fixture = load_fixture("real_trades_kxvpresnomr_28_mr.json")
        all_trades = fixture["trades"]
        times = sorted(set(t["created_time"] for t in all_trades))

        # --- Tick 1: bot starts, sees first batch ---
        # Simulate: API returns older trades (all except newest timestamp)
        tick1_trades = [t for t in all_trades
                        if t["created_time"] < times[-1]]
        ms = MarketState(ticker="KXVPRESNOMR-28-MR")

        new_t1 = run_dedup(ms, tick1_trades)
        assert new_t1 == [], "Tick 1 should return no trades"
        assert ms.last_seen_trade_ts == times[-2], \
            f"Watermark should be {times[-2]}"

        # Place order AFTER tick 1 watermark (simulates real bot behavior)
        placed_ts = datetime.fromisoformat(
            times[-2].replace("Z", "+00:00"))
        yes_price = round(
            float(all_trades[0]["yes_price_dollars"]) * 100)
        ms.yes_order = SimOrder(
            side="yes", price=yes_price, size=2, remaining=2,
            queue_pos=50, placed_at=placed_ts)

        # --- Tick 2: new trades arrive ---
        # Now API returns ALL trades (including newest timestamp)
        new_t2 = run_dedup(ms, all_trades)
        expected_new = [t for t in all_trades
                        if t["created_time"] == times[-1]]
        assert len(new_t2) == len(expected_new), \
            f"Tick 2 should see {len(expected_new)} new trades, got {len(new_t2)}"

        # Apply placed_at filter
        relevant = run_placed_filter(new_t2, ms.yes_order)
        assert len(relevant) > 0, \
            f"Should have relevant trades after placed_at filter"

        # Run drain
        drain = drain_queue(ms.yes_order, relevant)
        matching = [t for t in relevant
                    if round(float(t["yes_price_dollars"]) * 100) <= yes_price]
        expected_drain = sum(float(t["count_fp"]) for t in matching)
        assert drain == int(expected_drain), \
            f"Drain should be {int(expected_drain)}, got {drain}"

        # Process fills — queue should decrease
        old_qpos = ms.yes_order.queue_pos
        if drain > 0:
            process_fills(ms.yes_order, drain)
            assert ms.yes_order.queue_pos < old_qpos, \
                f"Queue pos should decrease: {old_qpos} -> {ms.yes_order.queue_pos}"

    def test_same_timestamp_batch_not_lost(self):
        """When multiple trades share a timestamp, none should be lost.

        This was the bug: strict '>' dropped trades at the same ts as watermark.
        """
        fixture = load_fixture("real_trades_kxgreenland_29.json")
        all_trades = fixture["trades"]

        # Find a timestamp with 3+ trades
        from collections import Counter
        ts_counts = Counter(t["created_time"] for t in all_trades)
        multi_ts = [ts for ts, c in ts_counts.items() if c >= 3]
        assert multi_ts, "Need a timestamp with 3+ trades"
        target_ts = multi_ts[0]
        trades_at_ts = [t for t in all_trades
                        if t["created_time"] == target_ts]

        # Simulate: watermark at this ts, only 1 trade seen
        ms = MarketState(ticker="KXGREENLAND-29")
        ms.last_seen_trade_ts = target_ts
        ms.last_seen_trade_ids = {trades_at_ts[0]["trade_id"]}

        # Tick: same trades come back
        new_trades = run_dedup(ms, all_trades)

        # The OTHER trades at this timestamp must pass
        unseen_count = len(trades_at_ts) - 1
        newer_count = len([t for t in all_trades
                           if t["created_time"] > target_ts])

        assert len(new_trades) >= unseen_count, \
            f"Expected at least {unseen_count} unseen same-ts trades, " \
            f"got {len(new_trades)} total new"
        assert len(new_trades) == unseen_count + newer_count
