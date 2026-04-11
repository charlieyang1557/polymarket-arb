# src/mm/state.py
"""Data model for the paper market maker."""

from __future__ import annotations
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import NamedTuple


class ExitLadderStep(NamedTuple):
    """One step in the progressive exit pricing ladder.
    seconds_threshold: trigger when seconds_to_game <= this
    price_offset: cents to add to fair_value (negative = try to profit)
    """
    seconds_threshold: int
    price_offset: int


DEFAULT_EXIT_LADDER: tuple[ExitLadderStep, ...] = (
    ExitLadderStep(seconds_threshold=1800, price_offset=-1),
    ExitLadderStep(seconds_threshold=1500, price_offset=0),
    ExitLadderStep(seconds_threshold=1200, price_offset=0),
    ExitLadderStep(seconds_threshold=600, price_offset=2),
    ExitLadderStep(seconds_threshold=300, price_offset=3),
)

TAKER_CROSS_SECONDS: int = 300


def dynamic_spread(midpoint_history: list[tuple[datetime, float]],
                   now: datetime, min_spread: int = 2,
                   lookback_min: int = 5) -> int:
    """Volatility-based spread: wider when price is swinging."""
    recent = [mid for ts, mid in midpoint_history
              if ts > now - timedelta(minutes=lookback_min)]
    if len(recent) < 3:
        return min_spread
    vol = statistics.stdev(recent)
    return max(min_spread, round(vol * 2))


def obi_microprice(best_bid: int, best_ask: int,
                   yes_depth: int, no_depth: int) -> float:
    """Order Book Imbalance micro-price.

    p_fair = best_bid + spread * (no_depth / (yes_depth + no_depth))

    When NO side is heavier, fair price shifts toward ask (higher).
    When YES side is heavier, fair price shifts toward bid (lower).
    Falls back to midpoint if both depths are zero.
    """
    spread = best_ask - best_bid
    total = yes_depth + no_depth
    if total == 0:
        return (best_bid + best_ask) / 2
    return best_bid + spread * (no_depth / total)


def skewed_quotes(fair: float, best_yes_bid: int, best_no_bid: int,
                  net_inventory: int, gamma: float = 0.5,
                  quote_offset: int = 0) -> tuple[int, int]:
    """Compute skewed bid prices for YES and NO sides.

    Anchors to OBI fair value (not BBO). Quotes are placed at:
      YES bid = fair - half_spread - quote_offset - skew
      NO bid  = (100-fair) - half_spread - quote_offset + skew

    Where half_spread = max(1, market_spread // 2).

    Positive net_inventory = long YES:
      skew > 0 → YES bid lower (less aggressive) + NO bid higher (more aggressive)

    Profitability floor (Polymarket): gross = 100 - yes - no >= 1c.
    Polymarket makers receive rebates, so no fee-based floor needed.
    """
    skew_raw = net_inventory * gamma

    # Derive half-spread from current book (= yes_ask - best_yes_bid) // 2
    market_spread = 100 - best_no_bid - best_yes_bid  # = yes_ask - best_yes_bid
    half_spread = max(1, market_spread // 2)

    yes_price = max(1, math.floor(fair - half_spread - quote_offset - skew_raw))
    no_price = max(1, math.floor((100 - fair) - half_spread - quote_offset + skew_raw))

    # Profitability floor: gross round-trip must be >= 1c
    # (Polymarket makers earn rebates — no positive fee cost to cover)
    while (100 - yes_price - no_price) < 1 and abs(skew_raw) > 0.1:
        skew_raw *= 0.8
        yes_price = max(1, math.floor(fair - half_spread - quote_offset - skew_raw))
        no_price = max(1, math.floor((100 - fair) - half_spread - quote_offset + skew_raw))

    return yes_price, no_price


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


def hedge_urgency_offset(oldest_fill_time: datetime | None,
                         now: datetime | None = None) -> int:
    """Price improvement (cents) for hedging side based on time since fill.

    Graduated escalation:
      0-5 min:   0c (passive maker, preserve queue priority)
      5-10 min:  1c (improve price, still maker)
      10-15 min: 2c (accept breakeven)
      15+ min:   5c (aggressive — accept loss to avoid settlement risk)

    Returns offset to ADD to the reducing side's quote price.
    """
    if oldest_fill_time is None:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    elapsed_min = (now - oldest_fill_time).total_seconds() / 60
    if elapsed_min < 5:
        return 0
    if elapsed_min < 10:
        return 1
    if elapsed_min < 15:
        return 2
    return 5


def compute_gamma(oldest_fill_time: datetime | None,
                  now: datetime | None = None,
                  base: float = 0.5,
                  ramp: float = 0.05,
                  cap: float = 2.0) -> float:
    """Adaptive inventory-skew gamma based on fill age.

    Ramps up from base when holding unhedged inventory:
      0 min: 0.5c/contract (baseline)
      10 min: 1.0c/contract
      20 min: 1.5c/contract
      30+ min: capped at 2.0c/contract

    Supplements hedge_urgency_offset (which adds an absolute price improvement).
    This widens the passive skew so the reducing side naturally attracts fills.
    """
    if oldest_fill_time is None:
        return base
    if now is None:
        now = datetime.now(timezone.utc)
    elapsed_min = (now - oldest_fill_time).total_seconds() / 60
    return min(base + elapsed_min * ramp, cap)


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
    last_seen_trade_ts: str = ""        # created_time watermark
    last_seen_trade_ids: set = field(default_factory=set)  # trade_ids at watermark ts
    consecutive_losses: int = 0
    oldest_fill_time: datetime | None = None  # for L2 time-based checks
    skew_activated_at: datetime | None = None  # when inventory skewing started
    paused_until: datetime | None = None
    midpoint_history: list[tuple[datetime, float]] = field(default_factory=list)
    last_api_success: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    trade_volume_1min: int = 0  # trades at our price level in last 60s
    trade_timestamps: list[datetime] = field(default_factory=list)
    deactivation_reason: str | None = None  # reason market was deactivated
    consecutive_skip_ticks: int = 0  # consecutive empty orderbook ticks
    session_initial_midpoint: float | None = None  # set on first tick for drift detection
    game_start_utc: datetime | None = None  # from schedule, for time-based exit
    aggress_cooldown_yes: datetime | None = None  # post-AGGRESS_FLATTEN cooldown per side
    aggress_cooldown_no: datetime | None = None
    total_fills: int = 0
    paired_fills: int = 0
    quote_disabled_reason: str | None = None

    @property
    def is_live_game(self) -> bool:
        """Live-game if >50 trades in last 5 minutes."""
        if not self.trade_timestamps:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        recent = [t for t in self.trade_timestamps if t > cutoff]
        return len(recent) > 50

    @property
    def is_soft_close(self) -> bool:
        """Soft-close if >30 trades in last 5 min but not yet live-game (>50)."""
        if not self.trade_timestamps:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        recent = [t for t in self.trade_timestamps if t > cutoff]
        count = len(recent)
        return 30 < count <= 50

    @property
    def post_fill_cooldown_s(self) -> int:
        """Seconds to wait after a fill. 30s in live-game, 0 in pre-game."""
        return 30 if self.is_live_game else 0

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
