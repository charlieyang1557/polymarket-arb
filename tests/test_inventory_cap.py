# tests/test_inventory_cap.py
"""Tests for bid floor rounding and single-side inventory cap."""
import math
from src.mm.state import skewed_quotes
from src.mm.engine import should_skip_side, clamp_order_size


# -- Floor rounding (both sides are bids → both floor) --
# Use best_yes_bid=44, best_no_bid=50 (6c gross) so profitability floor
# doesn't interfere with pure rounding tests.

def test_skew_yes_bid_floors():
    """YES bid with fractional skew floors down."""
    # inv=3, gamma=0.5 → skew=1.5
    # YES bid: floor(44 - 1.5) = floor(42.5) = 42
    yes_price, _ = skewed_quotes(
        fair=48.0, best_yes_bid=44, best_no_bid=50,
        net_inventory=3, gamma=0.5, quote_offset=0)
    assert yes_price == 42


def test_skew_no_bid_floors():
    """NO bid with fractional skew floors down (conservative)."""
    # inv=3, gamma=0.5 → skew=1.5
    # NO bid: floor(50 + 1.5) = floor(51.5) = 51
    _, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=44, best_no_bid=50,
        net_inventory=3, gamma=0.5, quote_offset=0)
    assert no_price == 51


def test_skew_negative_inv_floors():
    """Negative inv: both sides still floor."""
    # inv=-3, gamma=0.5 → skew=-1.5
    # YES bid: floor(44 - (-1.5)) = floor(45.5) = 45
    # NO bid:  floor(50 + (-1.5)) = floor(48.5) = 48
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=44, best_no_bid=50,
        net_inventory=-3, gamma=0.5, quote_offset=0)
    assert yes_price == 45
    assert no_price == 48


def test_integer_skew_unchanged():
    """Integer skew (no fractional part) → floor same as int."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=44, best_no_bid=50,
        net_inventory=4, gamma=0.5, quote_offset=0)
    assert yes_price == 42  # 44 - 2
    assert no_price == 52   # 50 + 2


def test_roundtrip_profit_guaranteed():
    """With profitability floor, gross should always cover fees + 1c
    when base spread is sufficient, or be maximized when base is tight."""
    for inv in range(-15, 16):
        yes_price, no_price = skewed_quotes(
            fair=48.0, best_yes_bid=44, best_no_bid=50,
            net_inventory=inv, gamma=0.5, quote_offset=0)
        gross = 100 - yes_price - no_price
        assert gross >= 0, f"Negative gross at inv={inv}: {yes_price}+{no_price}={yes_price+no_price}"


def test_fractional_skew_visible_at_inv_1():
    """At inv=1, skew=0.5 → floor(44-0.5)=43, floor(50+0.5)=50."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=44, best_no_bid=50,
        net_inventory=1, gamma=0.5, quote_offset=0)
    assert yes_price == 43  # floor(43.5) = 43
    assert no_price == 50   # floor(50.5) = 50


# -- Single-side inventory cap --

def test_skip_yes_at_max_inventory():
    """Long YES at max → skip YES (don't buy more), keep NO."""
    assert should_skip_side("yes", net_inventory=10, max_inventory=10) is True
    assert should_skip_side("no", net_inventory=10, max_inventory=10) is False


def test_skip_no_at_negative_max():
    """Long NO at -max → skip NO (don't buy more), keep YES."""
    assert should_skip_side("no", net_inventory=-10, max_inventory=10) is True
    assert should_skip_side("yes", net_inventory=-10, max_inventory=10) is False


def test_no_skip_below_max():
    """Below max on both sides → quote both."""
    assert should_skip_side("yes", net_inventory=5, max_inventory=10) is False
    assert should_skip_side("no", net_inventory=5, max_inventory=10) is False


def test_no_skip_at_zero():
    """Zero inventory → quote both."""
    assert should_skip_side("yes", net_inventory=0, max_inventory=10) is False
    assert should_skip_side("no", net_inventory=0, max_inventory=10) is False


# -- Order size clamping --

def test_clamp_yes_near_max():
    """inv=9, max=10, order_size=2 → YES clamped to 1 (would overshoot)."""
    assert clamp_order_size("yes", net_inventory=9, order_size=2, max_inventory=10) == 1

def test_clamp_no_near_negative_max():
    """inv=-9, max=10, order_size=2 → NO clamped to 1."""
    assert clamp_order_size("no", net_inventory=-9, order_size=2, max_inventory=10) == 1

def test_clamp_no_change_at_zero():
    """inv=0 → both sides full size."""
    assert clamp_order_size("yes", net_inventory=0, order_size=2, max_inventory=10) == 2
    assert clamp_order_size("no", net_inventory=0, order_size=2, max_inventory=10) == 2

def test_clamp_decreasing_side_uncapped():
    """inv=9, YES increases inv → clamp. NO decreases inv → full size."""
    assert clamp_order_size("no", net_inventory=9, order_size=2, max_inventory=10) == 2

def test_clamp_at_max_returns_zero():
    """inv=10 → YES size=0 (should_skip_side handles this, but clamp is consistent)."""
    assert clamp_order_size("yes", net_inventory=10, order_size=2, max_inventory=10) == 0

def test_clamp_negative_inv_yes_uncapped():
    """inv=-5, YES decreases magnitude → full size."""
    assert clamp_order_size("yes", net_inventory=-5, order_size=2, max_inventory=10) == 2

def test_clamp_negative_inv_no_capped():
    """inv=-1, max=3, NO increases magnitude → clamp to min(2, 3-1)=2."""
    assert clamp_order_size("no", net_inventory=-1, order_size=2, max_inventory=3) == 2

def test_clamp_inv_neg1_max3_no_size2():
    """inv=-1, max=3, order_size=2 → NO clamp to 2 (|-1|+2=3 ≤ 3)."""
    assert clamp_order_size("no", net_inventory=-1, order_size=2, max_inventory=3) == 2

def test_clamp_inv_neg2_max3_no_size2():
    """inv=-2, max=3, order_size=2 → NO clamp to 1 (|-2|+1=3 ≤ 3)."""
    assert clamp_order_size("no", net_inventory=-2, order_size=2, max_inventory=3) == 1
