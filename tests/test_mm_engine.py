# tests/test_mm_engine.py
from datetime import datetime, timezone
from src.mm.engine import drain_queue, process_fills, pair_off_inventory
from src.mm.state import SimOrder, MarketState


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
