#!/usr/bin/env python3
"""Integration test: verify drain_queue works with real Kalshi trade data.

Tests the exact code path: dedup filtering -> drain_queue -> process_fills
using REAL API data, not mocks.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from src.kalshi_client import KalshiClient, PROD_BASE
from src.mm.state import SimOrder, MarketState
from src.mm.engine import drain_queue, process_fills

TICKER = "KXGREENLAND-29"


def main():
    client = KalshiClient(
        os.getenv("KALSHI_API_KEY"),
        os.getenv("KALSHI_PRIVATE_KEY_PATH"),
        PROD_BASE,
    )

    # 1. Fetch recent trades
    data = client.get_trades(TICKER, limit=10)
    trades = data.get("trades", [])
    print(f"1. Fetched {len(trades)} trades for {TICKER}")
    for i, t in enumerate(trades):
        yes_c = round(float(t["yes_price_dollars"]) * 100)
        vol = float(t["count_fp"])
        print(f"   [{i}] {t['created_time']}  yes={yes_c}c  "
              f"vol={vol:.0f}  id={t['trade_id'][:12]}...")

    assert len(trades) >= 2, "Need at least 2 trades"

    # Find two trades with DIFFERENT created_time values
    # (trades come newest-first from API)
    times = sorted(set(t["created_time"] for t in trades))
    assert len(times) >= 2, \
        f"Need 2+ distinct timestamps, got {len(times)}: {times}"

    older_ts = times[-2]  # second-newest timestamp
    newer_ts = times[-1]  # newest timestamp

    newer_trades = [t for t in trades if t["created_time"] == newer_ts]
    older_trades = [t for t in trades if t["created_time"] == older_ts]

    print(f"\n2. Watermark scenario:")
    print(f"   older_ts = '{older_ts}' ({len(older_trades)} trades)")
    print(f"   newer_ts = '{newer_ts}' ({len(newer_trades)} trades)")

    # --- Test A: standard case (newer timestamp strictly after watermark) ---
    print(f"\n{'='*60}")
    print("TEST A: Trades with newer timestamp pass dedup filter")
    print(f"{'='*60}")

    ms = MarketState(ticker=TICKER)
    # Simulate: we already saw all older trades
    ms.last_seen_trade_ts = older_ts
    ms.last_seen_trade_ids = {t["trade_id"] for t in older_trades}

    # Apply the same dedup logic as engine.py
    wm = ms.last_seen_trade_ts
    new_trades = [
        t for t in trades
        if t.get("created_time", "") > wm
        or (t.get("created_time", "") == wm
            and t.get("trade_id") not in ms.last_seen_trade_ids)
    ]
    print(f"   new_trades after dedup = {len(new_trades)}")
    assert len(new_trades) == len(newer_trades), \
        f"Expected {len(newer_trades)} new trades, got {len(new_trades)}"

    # Pick a price that matches the newest trade for drain
    target_yes = round(float(newer_trades[0]["yes_price_dollars"]) * 100)
    order = SimOrder(
        side="yes", price=target_yes, size=2, remaining=2,
        queue_pos=100,
        placed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    drain = drain_queue(order, new_trades)
    matching = [t for t in new_trades
                if round(float(t["yes_price_dollars"]) * 100) <= target_yes]
    expected_vol = sum(float(t["count_fp"]) for t in matching)
    print(f"   order: yes@{target_yes}c, queue_pos=100")
    print(f"   matching trades at yes<={target_yes}c: {len(matching)}, "
          f"vol={expected_vol:.0f}")
    print(f"   drain_queue returned: {drain}")

    old_qpos = order.queue_pos
    if drain > 0:
        filled = process_fills(order, drain)
        print(f"   queue_pos: {old_qpos} -> {order.queue_pos}")
        print(f"   filled: {filled}")

    assert drain > 0, f"FAIL: drain=0 for yes@{target_yes}c"
    assert order.queue_pos < old_qpos, \
        f"FAIL: queue_pos didn't decrease ({old_qpos} -> {order.queue_pos})"
    print("   ✅ PASS")

    # --- Test B: same-timestamp dedup (the bug that was killing us) ---
    print(f"\n{'='*60}")
    print("TEST B: Same-timestamp trades with unseen trade_ids pass filter")
    print(f"{'='*60}")

    if len(newer_trades) >= 2:
        ms2 = MarketState(ticker=TICKER)
        # Simulate: we saw only the FIRST trade at newer_ts
        ms2.last_seen_trade_ts = newer_ts
        ms2.last_seen_trade_ids = {newer_trades[0]["trade_id"]}

        wm2 = ms2.last_seen_trade_ts
        new_trades2 = [
            t for t in trades
            if t.get("created_time", "") > wm2
            or (t.get("created_time", "") == wm2
                and t.get("trade_id") not in ms2.last_seen_trade_ids)
        ]
        print(f"   Watermark at newest_ts, seen 1/{len(newer_trades)} trade_ids")
        print(f"   new_trades after dedup = {len(new_trades2)}")
        assert len(new_trades2) == len(newer_trades) - 1, \
            f"Expected {len(newer_trades) - 1}, got {len(new_trades2)}"
        print("   ✅ PASS")
    else:
        print("   SKIP (only 1 trade at newest timestamp)")

    # --- Test C: no dupes when fully caught up ---
    print(f"\n{'='*60}")
    print("TEST C: No dupes when fully caught up")
    print(f"{'='*60}")

    ms3 = MarketState(ticker=TICKER)
    ms3.last_seen_trade_ts = newer_ts
    ms3.last_seen_trade_ids = {t["trade_id"] for t in newer_trades}

    wm3 = ms3.last_seen_trade_ts
    new_trades3 = [
        t for t in trades
        if t.get("created_time", "") > wm3
        or (t.get("created_time", "") == wm3
            and t.get("trade_id") not in ms3.last_seen_trade_ids)
    ]
    print(f"   Fully caught up at newest_ts with all {len(newer_trades)} ids")
    print(f"   new_trades after dedup = {len(new_trades3)}")
    assert len(new_trades3) == 0, f"Expected 0, got {len(new_trades3)}"
    print("   ✅ PASS")

    print(f"\n{'='*60}")
    print("ALL TESTS PASSED")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
