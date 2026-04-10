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
    SOFT_CLOSE = 2
    PAUSE_30MIN = 3
    PAUSE_60S = 4
    SKEW_QUOTES = 5
    AGGRESS_FLATTEN = 6
    FORCE_CLOSE = 7
    STOP_AND_FLATTEN = 8
    CANCEL_ALL = 9
    EXIT_MARKET = 10
    FULL_STOP = 11


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

def check_layer2(ms: MarketState,
                 max_inventory: int = 10,
                 aggress_threshold: int | None = None) -> Action:
    net = abs(ms.net_inventory)
    now = datetime.now(timezone.utc)

    if aggress_threshold is None:
        aggress_threshold = max(2, int(max_inventory * 0.8))

    # Time-based checks on oldest unhedged position
    # Only escalate if inventory is meaningful (> half of aggress threshold)
    time_thresh = max(2, aggress_threshold // 2)
    if ms.oldest_fill_time and net > time_thresh:
        age = now - ms.oldest_fill_time
        if age > timedelta(hours=4):
            return Action.FORCE_CLOSE
        if age > timedelta(hours=2):
            return Action.AGGRESS_FLATTEN

    # Emergency backstops
    stop_thresh = max(4, int(max_inventory * 2.5))
    if net > stop_thresh:
        return Action.STOP_AND_FLATTEN
    if net > max_inventory:
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

    # Time-based game start: exit/soft-close based on schedule
    if ms.game_start_utc is not None:
        seconds_to_start = (ms.game_start_utc - now).total_seconds()
        if seconds_to_start <= 0:
            return Action.EXIT_MARKET
        if seconds_to_start < 1800:
            return Action.SOFT_CLOSE

    # Session drift: 10c+ from initial midpoint → EXIT_MARKET
    if ms.session_initial_midpoint is not None and ms.midpoint_history:
        current_mid = ms.midpoint_history[-1][1]
        drift = abs(current_mid - ms.session_initial_midpoint)
        if drift > 10:
            return Action.EXIT_MARKET

    # Price jump detection: 3c threshold in live-game, 5c in pre-game
    threshold = 3 if ms.is_live_game else 5
    if len(ms.midpoint_history) >= 2:
        oldest_entry = ms.midpoint_history[0]
        newest_entry = ms.midpoint_history[-1]
        time_diff = (newest_entry[0] - oldest_entry[0]).total_seconds()
        if time_diff <= 65 and abs(newest_entry[1] - oldest_entry[1]) > threshold:
            return Action.PAUSE_60S

    return Action.CONTINUE
