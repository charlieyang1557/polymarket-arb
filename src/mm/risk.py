# src/mm/risk.py
"""Risk management layers 1-4 for the paper market maker."""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from src.mm.state import MarketState, GlobalState


class Action(IntEnum):
    """Risk actions ordered by priority (highest = most restrictive)."""
    CONTINUE = 0
    SKIP_TICK = 1
    PAUSE_30MIN = 2
    PAUSE_60S = 3
    SKEW_QUOTES = 4
    AGGRESS_FLATTEN = 5
    FORCE_CLOSE = 6
    STOP_AND_FLATTEN = 7
    CANCEL_ALL = 8
    EXIT_MARKET = 9
    FULL_STOP = 10


def highest_priority(actions: list[Action]) -> Action:
    return max(actions) if actions else Action.CONTINUE


# -- Layer 1: Per-Order Validation -----------------------------------------

def check_layer1(price: int, size: int, midpoint: float,
                 max_size: int = 5, side: str = "yes") -> str | None:
    """Returns rejection reason string, or None if valid.

    For YES bids, compare price against YES midpoint.
    For NO bids, compare price against NO midpoint (100 - midpoint).
    """
    if size > max_size:
        return f"size {size} > max {max_size}"
    ref = midpoint if side == "yes" else (100 - midpoint)
    if ref > 0 and abs(price - ref) > ref * 0.10:
        return f"price {price} outside ±10% of {side} ref {ref:.1f}"
    return None


# -- Layer 2: Inventory Management ----------------------------------------

def check_layer2(ms: MarketState) -> Action:
    net = abs(ms.net_inventory)
    now = datetime.now(timezone.utc)

    # Time-based checks on oldest unhedged position
    # Only escalate if inventory is meaningful (> order size)
    if ms.oldest_fill_time and net > 2:
        age = now - ms.oldest_fill_time
        if age > timedelta(hours=4):
            return Action.FORCE_CLOSE
        if age > timedelta(hours=2):
            return Action.AGGRESS_FLATTEN

    # Emergency backstops (continuous skew handles inv < 20)
    if net > 20:
        return Action.STOP_AND_FLATTEN
    if net > 10:
        return Action.AGGRESS_FLATTEN
    return Action.CONTINUE


# -- Layer 3: P&L Circuit Breakers ----------------------------------------

def check_layer3(ms: MarketState, gs: GlobalState) -> Action:
    # Collect all triggered actions, return highest priority.
    actions = []

    # Daily loss across all markets
    if gs.total_realized_pnl < -500:
        actions.append(Action.FULL_STOP)

    # Drawdown gate: only trigger when session is net-negative
    peak = gs.peak_total_pnl
    current = gs.total_pnl
    drawdown = peak - current
    if current < 0 and peak > 100 and drawdown > 50 and drawdown / peak > 0.05:
        actions.append(Action.FULL_STOP)

    # Per-market cumulative loss
    if ms.realized_pnl < -1000:
        actions.append(Action.EXIT_MARKET)

    # Consecutive losses
    if ms.consecutive_losses >= 3:
        actions.append(Action.PAUSE_30MIN)

    return highest_priority(actions) if actions else Action.CONTINUE


def apply_pause_30min(ms: MarketState):
    """Apply PAUSE_30MIN action: set pause timer and reset consecutive_losses
    so the bot doesn't loop forever re-triggering the same pause."""
    now = datetime.now(timezone.utc)
    ms.paused_until = now + timedelta(minutes=30)
    ms.consecutive_losses = 0


# -- Layer 4: System Risk -------------------------------------------------

def check_layer4(ms: MarketState, spread: int,
                 db_error_count: int) -> Action:
    # DB write failures
    if db_error_count >= 10:
        return Action.FULL_STOP

    # Crossed book
    if spread <= 0:
        return Action.SKIP_TICK

    # API disconnect
    now = datetime.now(timezone.utc)
    if (now - ms.last_api_success) > timedelta(seconds=30):
        return Action.CANCEL_ALL

    # Price jump detection: 3c threshold in live-game, 5c in pre-game
    threshold = 3 if ms.is_live_game else 5
    if len(ms.midpoint_history) >= 2:
        oldest_entry = ms.midpoint_history[0]
        newest_entry = ms.midpoint_history[-1]
        time_diff = (newest_entry[0] - oldest_entry[0]).total_seconds()
        if time_diff <= 65 and abs(newest_entry[1] - oldest_entry[1]) > threshold:
            return Action.PAUSE_60S

    return Action.CONTINUE
