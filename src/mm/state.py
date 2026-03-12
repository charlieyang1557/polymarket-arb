# src/mm/state.py
"""Data model for the paper market maker."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone


def maker_fee_cents(price_cents: int, count: int) -> float:
    """Kalshi maker fee in cents. Formula: 0.0175 * count * P * (1-P) * 100."""
    p = price_cents / 100
    return 0.0175 * count * p * (1 - p) * 100


def taker_fee_cents(price_cents: int, count: int) -> float:
    """Kalshi taker fee in cents. Formula: 0.07 * count * P * (1-P) * 100."""
    p = price_cents / 100
    return 0.07 * count * p * (1 - p) * 100


def unrealized_pnl_cents(yes_queue: list[int], no_queue: list[int],
                         best_yes_bid: int, best_no_bid: int) -> float:
    """Conservative mark-to-market unrealized P&L for unhedged inventory.

    Uses exit prices (bids), NOT midpoint, to avoid phantom profits
    in wide-spread markets. YES valued at best_yes_bid, NO at best_no_bid.
    """
    if len(yes_queue) > len(no_queue):
        unhedged = yes_queue[len(no_queue):]
        return sum(best_yes_bid - cost for cost in unhedged)
    elif len(no_queue) > len(yes_queue):
        unhedged = no_queue[len(yes_queue):]
        return sum(best_no_bid - cost for cost in unhedged)
    return 0.0


@dataclass
class SimOrder:
    """A simulated resting order."""
    side: str           # "yes" or "no"
    price: int          # cents
    size: int
    remaining: int
    queue_pos: int      # contracts ahead of us
    placed_at: datetime
    last_drain_trade_id: str = ""  # per-order trade dedup for queue drain
    db_id: int | None = None  # mm_orders row id once persisted


@dataclass
class MarketState:
    """Per-market state for the paper MM."""
    ticker: str
    active: bool = True
    yes_order: SimOrder | None = None
    no_order: SimOrder | None = None
    yes_queue: list[int] = field(default_factory=list)
    no_queue: list[int] = field(default_factory=list)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees: float = 0.0
    last_seen_trade_id: str = ""
    consecutive_losses: int = 0
    oldest_fill_time: datetime | None = None  # for L2 time-based checks
    skew_activated_at: datetime | None = None  # when inventory skewing started
    paused_until: datetime | None = None
    midpoint_history: list[tuple[datetime, float]] = field(default_factory=list)
    last_api_success: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    trade_volume_1min: int = 0  # trades at our price level in last 60s

    @property
    def net_inventory(self) -> int:
        """Positive = long YES, negative = long NO."""
        return len(self.yes_queue) - len(self.no_queue)


@dataclass
class GlobalState:
    """Aggregate state across all markets."""
    markets: dict[str, MarketState] = field(default_factory=dict)
    start_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    session_id: str = ""
    db_error_count: int = 0
    peak_total_pnl: float = 0.0

    @property
    def total_realized_pnl(self) -> float:
        return sum(m.realized_pnl for m in self.markets.values())

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(m.unrealized_pnl for m in self.markets.values())

    @property
    def total_pnl(self) -> float:
        return self.total_realized_pnl + self.total_unrealized_pnl
