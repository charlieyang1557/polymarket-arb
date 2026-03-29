# tests/test_poly_risk_fixes.py
"""Tests for Polymarket-specific risk fixes: inv clamping, cooldowns, safe exit."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from src.mm.engine import clamp_order_size, should_skip_side, MAX_INVENTORY
from src.mm.risk import check_layer2, Action
from src.mm.state import MarketState


# --- Fix 1A: AGGRESS_FLATTEN thresholds ---

def test_l2_no_aggress_at_inv4():
    """inv=4 held 2.5h should NOT trigger AGGRESS_FLATTEN (too small)."""
    ms = MarketState(ticker="test")
    ms.yes_queue = [50] * 4  # inv = +4
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
    action = check_layer2(ms)
    assert action == Action.CONTINUE


def test_l2_aggress_at_inv6_after_2h():
    """inv=6 held >2h SHOULD trigger AGGRESS_FLATTEN (meaningful position)."""
    ms = MarketState(ticker="test")
    ms.yes_queue = [50] * 6  # inv = +6
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
    action = check_layer2(ms)
    assert action == Action.AGGRESS_FLATTEN


def test_l2_no_aggress_inv6_under_2h():
    """inv=6 held only 1h — no action (not stale yet)."""
    ms = MarketState(ticker="test")
    ms.yes_queue = [50] * 6
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=1)
    action = check_layer2(ms)
    assert action == Action.CONTINUE


def test_l2_stop_and_flatten_inv30():
    """Inventory >25 → STOP_AND_FLATTEN."""
    ms = MarketState(ticker="test")
    ms.yes_queue = [50] * 30
    action = check_layer2(ms)
    assert action == Action.STOP_AND_FLATTEN


def test_l2_aggress_inv15():
    """Inventory >MAX_INVENTORY(10) → AGGRESS_FLATTEN."""
    ms = MarketState(ticker="test")
    ms.yes_queue = [50] * 15
    action = check_layer2(ms)
    assert action == Action.AGGRESS_FLATTEN


# --- Fix 1B: Inventory clamping ---

def test_clamp_risk_side_at_limit():
    """max_inv=10, inv=10, size=2 → risk-side = 0."""
    size = clamp_order_size("yes", net_inventory=10, order_size=2,
                             max_inventory=MAX_INVENTORY)
    assert size == 0


def test_clamp_risk_side_near_limit():
    """max_inv=10, inv=9, size=2 → risk-side = 1."""
    size = clamp_order_size("yes", net_inventory=9, order_size=2,
                             max_inventory=MAX_INVENTORY)
    assert size == 1


def test_clamp_safe_side_no_limit():
    """max_inv=10, inv=-3, size=2 → both sides = 2."""
    yes_size = clamp_order_size("yes", net_inventory=-3, order_size=2)
    no_size = clamp_order_size("no", net_inventory=-3, order_size=2)
    assert yes_size == 2  # YES reduces |inv|
    assert no_size == 2   # NO at |-3| < 10, room for 7 more


def test_clamp_prevents_aggress_trigger():
    """With clamping, inventory can never exceed MAX_INVENTORY from quoting."""
    inv = 8
    for _ in range(10):
        size = clamp_order_size("yes", net_inventory=inv, order_size=2)
        if size > 0:
            inv += size
        assert inv <= MAX_INVENTORY


# --- Fix 2: Soft-close aggressive maker + safe exit ---

def test_soft_close_aggressive_maker_price():
    """During SOFT_CLOSE, aggressive maker crosses spread by 1-2c."""
    from src.mm.engine import soft_close_exit_price
    # inv=+4, YES side (need to sell YES = buy NO aggressively)
    # fair=50, best_no_bid=48 → aggressive NO bid = 49 (1c above best)
    price = soft_close_exit_price(
        side="no", fair_value=50, best_bid=48, max_slippage=5)
    assert price == 49  # 1c above best bid
    assert price <= 50 + 5  # within max_slippage of fair


def test_soft_close_price_capped():
    """Aggressive maker price capped at fair + max_slippage."""
    price = soft_close_exit_price(
        side="no", fair_value=50, best_bid=58, max_slippage=5)
    # best_bid=58 already above fair. Cap at fair + max_slippage = 55
    assert price <= 55


def test_soft_close_exit_price_yes():
    """YES aggressive maker: crosses ask."""
    # inv=-4, need to buy YES aggressively
    # fair=50, best_yes_bid=48 → aggressive = 49
    price = soft_close_exit_price(
        side="yes", fair_value=50, best_bid=48, max_slippage=5)
    assert price == 49


# --- Fix 3: Post-AGGRESS_FLATTEN cooldown ---

def test_cooldown_field_exists():
    """MarketState has cooldown_until fields per side."""
    ms = MarketState(ticker="test")
    assert ms.aggress_cooldown_yes is None
    assert ms.aggress_cooldown_no is None


def test_cooldown_blocks_quoting():
    """After AGGRESS_FLATTEN on YES side, yes quoting blocked for 30s."""
    ms = MarketState(ticker="test")
    now = datetime.now(timezone.utc)
    ms.aggress_cooldown_yes = now + timedelta(seconds=30)

    # YES side should be blocked
    assert is_side_cooled_down(ms, "yes", now)
    # NO side should NOT be blocked
    assert not is_side_cooled_down(ms, "no", now)


def test_cooldown_expires():
    """After 30s, cooldown expires."""
    ms = MarketState(ticker="test")
    now = datetime.now(timezone.utc)
    ms.aggress_cooldown_yes = now - timedelta(seconds=1)  # expired
    assert not is_side_cooled_down(ms, "yes", now)


# Import after defining tests to avoid circular issues
from src.mm.engine import is_side_cooled_down, soft_close_exit_price
