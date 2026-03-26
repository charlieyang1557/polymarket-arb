# tests/test_mm_engine.py
import json
import os
from datetime import datetime, timezone
from src.mm.engine import drain_queue, process_fills, pair_off_inventory
from src.mm.state import SimOrder, MarketState, GlobalState


def test_drain_queue_yes_bid():
    """Trades at or below our YES bid price drain the queue."""
    order = SimOrder(side="yes", price=26, size=2, remaining=2,
                     queue_pos=42,
                     placed_at=datetime.now(timezone.utc))
    # Simulated trades: 15 contracts at yes_price <= 26
    trades = [{"trade_id": "t1", "count_fp": "15.0",
               "yes_price_dollars": "0.2500",
               "created_time": datetime.now(timezone.utc).isoformat()}]
    drain = drain_queue(order, trades)
    assert drain == 15


def test_drain_queue_no_bid():
    """NO bid drains from trades where (100 - yes_price) <= NO bid price."""
    order = SimOrder(side="no", price=69, size=2, remaining=2,
                     queue_pos=30,
                     placed_at=datetime.now(timezone.utc))
    # Trade at yes_price=30c -> no_price=70c. 70 > 69, does NOT drain.
    trades_no_drain = [{"trade_id": "t1", "count_fp": "10.0",
                        "yes_price_dollars": "0.3000",
                        "created_time": datetime.now(timezone.utc).isoformat()}]
    assert drain_queue(order, trades_no_drain) == 0

    # Trade at yes_price=32c -> no_price=68c. 68 <= 69, drains.
    trades_drain = [{"trade_id": "t2", "count_fp": "10.0",
                     "yes_price_dollars": "0.3200",
                     "created_time": datetime.now(timezone.utc).isoformat()}]
    assert drain_queue(order, trades_drain) == 10


def test_process_fills_full():
    """Queue drains past zero -> fill our order."""
    order = SimOrder(side="yes", price=26, size=2, remaining=2,
                     queue_pos=5,
                     placed_at=datetime.now(timezone.utc))
    filled = process_fills(order, drain=8)
    assert filled == 2  # min(remaining=2, max(0, 8-5)=3) -> 2
    assert order.remaining == 0
    assert order.queue_pos == 0


def test_process_fills_partial():
    """Queue partially drains -> partial fill."""
    order = SimOrder(side="yes", price=26, size=5, remaining=5,
                     queue_pos=2,
                     placed_at=datetime.now(timezone.utc))
    filled = process_fills(order, drain=4)
    assert filled == 2  # min(5, max(0, 4-2)) = 2
    assert order.remaining == 3
    assert order.queue_pos == 0


def test_process_fills_no_fill():
    """Drain doesn't reach our queue position."""
    order = SimOrder(side="yes", price=26, size=2, remaining=2,
                     queue_pos=42,
                     placed_at=datetime.now(timezone.utc))
    filled = process_fills(order, drain=10)
    assert filled == 0
    assert order.queue_pos == 32


def test_pair_off_inventory():
    """Matched YES+NO pairs settle at 100c."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [26, 28]
    ms.no_queue = [69]
    # Should pair first YES(26) + first NO(69)
    pairs = pair_off_inventory(ms)
    assert len(pairs) == 1
    gross = 100 - 26 - 69  # = 5c
    assert pairs[0]["gross_pnl"] == gross
    assert len(ms.yes_queue) == 1  # [28] remains
    assert len(ms.no_queue) == 0


# -- Hot-add pending markets --------------------------------------------------

def test_engine_loads_pending_markets(tmp_path):
    """Engine picks up pending_markets.json and creates MarketState."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    gs.markets["EXISTING"] = MarketState(ticker="EXISTING")

    pending = [
        {"ticker": "NEW1", "game_start_utc": "2026-03-26T23:00:00Z"},
        {"ticker": "NEW2"},
    ]
    pending_path = str(tmp_path / "pending_markets.json")
    with open(pending_path, "w") as f:
        json.dump(pending, f)

    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == ["NEW1", "NEW2"]
    assert "NEW1" in gs.markets
    assert "NEW2" in gs.markets
    assert gs.markets["NEW1"].game_start_utc is not None
    assert gs.markets["NEW2"].game_start_utc is None
    # Both original and .processing files should be gone
    assert not os.path.exists(pending_path)
    assert not os.path.exists(pending_path + ".processing")


def test_engine_skips_duplicate_tickers(tmp_path):
    """Tickers already in gs.markets are skipped."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    gs.markets["DUP"] = MarketState(ticker="DUP")

    pending_path = str(tmp_path / "pending_markets.json")
    with open(pending_path, "w") as f:
        json.dump([{"ticker": "DUP"}, {"ticker": "FRESH"}], f)

    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == ["FRESH"]
    assert len(gs.markets) == 2  # DUP + FRESH


def test_engine_respects_active_market_cap(tmp_path):
    """Cap counts only active markets; exited markets don't count."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    # 10 exited + 4 active = 14 total, 4 active
    for i in range(10):
        ms = MarketState(ticker=f"EXIT{i}")
        ms.active = False
        gs.markets[f"EXIT{i}"] = ms
    for i in range(4):
        gs.markets[f"ACTIVE{i}"] = MarketState(ticker=f"ACTIVE{i}")

    pending_path = str(tmp_path / "pending_markets.json")
    new = [{"ticker": f"NEW{i}"} for i in range(12)]
    with open(pending_path, "w") as f:
        json.dump(new, f)

    added = load_pending_markets(gs, pending_path, max_active=15)
    # 4 active + 11 new = 15 (cap). Only 11 should be added.
    assert len(added) == 11
    active_count = sum(1 for m in gs.markets.values() if m.active)
    assert active_count == 15


def test_engine_handles_malformed_pending(tmp_path):
    """Malformed JSON doesn't crash — logs warning, skips."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    pending_path = str(tmp_path / "pending_markets.json")
    with open(pending_path, "w") as f:
        f.write("{invalid json")

    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == []
    # Both original and .processing files should be gone
    assert not os.path.exists(pending_path)
    assert not os.path.exists(pending_path + ".processing")


def test_engine_no_pending_file(tmp_path):
    """No pending file → empty list, no error."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    added = load_pending_markets(gs, str(tmp_path / "nope.json"), max_active=15)
    assert added == []


def test_engine_atomic_consume_preserves_new_file(tmp_path):
    """Scanner can write a new pending file while engine processes the old one.

    Simulates: engine renames to .processing, scanner writes new file,
    engine deletes .processing — new file survives.
    """
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    pending_path = str(tmp_path / "pending_markets.json")

    # Engine's first pending file
    with open(pending_path, "w") as f:
        json.dump([{"ticker": "BATCH1"}], f)

    # Engine renames to .processing (simulated by calling load_pending_markets)
    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == ["BATCH1"]

    # Scanner writes a NEW file at the same path after engine consumed the old one
    with open(pending_path, "w") as f:
        json.dump([{"ticker": "BATCH2"}], f)

    # The new file should survive — engine only deleted .processing
    assert os.path.exists(pending_path)

    # Engine picks up the new file on next check
    added2 = load_pending_markets(gs, pending_path, max_active=15)
    assert added2 == ["BATCH2"]
