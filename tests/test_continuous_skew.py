# tests/test_continuous_skew.py
"""Tests for continuous inventory skew quoting."""
import math
from src.mm.state import skewed_quotes, maker_fee_cents

GAMMA = 0.5  # cents per contract

# All tests use best_yes_bid=45, best_no_bid=50 (5c gross spread)
# to ensure profitability floor doesn't interfere with pure skew tests.


# -- Basic skew behavior --

def test_skew_zero_at_zero_inventory():
    """No inventory → no skew, quotes symmetric around fair price."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=0, gamma=GAMMA, quote_offset=0)
    assert yes_price == 45
    assert no_price == 50


def test_skew_positive_inventory():
    """Long YES (inv=4) → lower YES bid, raise NO bid to attract NO fills.

    skew = 4 * 0.5 = 2c
    YES bid: 45 - 2 = 43
    NO bid:  50 + 2 = 52
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 43
    assert no_price == 52


def test_skew_negative_inventory():
    """Long NO (inv=-4) → raise YES bid, lower NO bid to attract YES fills.

    skew = -4 * 0.5 = -2c
    YES bid: 45 - (-2) = 47
    NO bid:  50 + (-2) = 48
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=-4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 47
    assert no_price == 48


def test_skew_at_inv_10():
    """At inv=10, skew=5c — significant but not extreme.
    yes=40, no=55, gross=5 → still profitable, floor doesn't trigger."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=10, gamma=GAMMA, quote_offset=0)
    assert yes_price == 40  # 45 - 5
    assert no_price == 55   # 50 + 5


def test_skew_floor_at_1c():
    """Skew can't push price below 1c."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=3, best_no_bid=50,
        net_inventory=20, gamma=GAMMA, quote_offset=0)
    # skew = 10c, 3 - 10 = -7 → clamp to 1
    assert yes_price == 1


def test_skew_with_quote_offset():
    """Live-game offset stacks with skew."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=4, gamma=GAMMA, quote_offset=2)
    # offset=2, skew=2: yes = 45 - 2 - 2 = 41, no = 50 - 2 + 2 = 50
    assert yes_price == 41
    assert no_price == 50


def test_skew_small_inventory():
    """At inv=1, skew=0.5c → floor makes YES visible, NO unchanged."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=1, gamma=GAMMA, quote_offset=0)
    # skew=0.5: floor(44.5)=44, floor(50.5)=50
    assert yes_price == 44
    assert no_price == 50


def test_skew_inv_2_visible():
    """At inv=2, skew=1c — first visible adjustment."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    assert yes_price == 44  # 45 - 1
    assert no_price == 51   # 50 + 1


# -- Profitability floor --

def test_profitability_floor_reduces_extreme_skew():
    """Tight spread (2c gross) + high inv should trigger floor.
    best_yes_bid=48, best_no_bid=50, sum=98, gross=2.
    inv=8, skew=4: yes=44, no=54, sum=98, gross=2.
    min_fees at mid~49 = 2*ceil(0.0175*49*51/100) = 2*1 = 2c.
    Need gross >= 3. Floor should reduce skew."""
    yes_price, no_price = skewed_quotes(
        fair=49.0, best_yes_bid=48, best_no_bid=50,
        net_inventory=8, gamma=GAMMA, quote_offset=0)
    gross = 100 - yes_price - no_price
    mid = (yes_price + no_price) / 2
    min_fees = 2 * math.ceil(0.0175 * mid / 100 * (1 - mid / 100) * 100)
    assert gross >= min_fees + 1, f"gross={gross}, min_fees={min_fees}"


def test_profitability_floor_preserves_skew_direction():
    """Floor reduces skew magnitude but preserves direction.
    Long YES (inv>0) → YES bid still reduced, NO bid still raised."""
    yes_price, no_price = skewed_quotes(
        fair=49.0, best_yes_bid=48, best_no_bid=50,
        net_inventory=8, gamma=GAMMA, quote_offset=0)
    # Direction preserved even after floor
    assert yes_price <= 48  # still trying to bid lower on YES
    assert no_price >= 50   # still trying to bid higher on NO


def test_profitability_floor_does_not_affect_small_skew():
    """Wide spread + small skew = already profitable, floor is no-op."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    # 5c gross spread, skew=1c doesn't threaten profitability
    assert yes_price == 44  # 45 - 1
    assert no_price == 51   # 50 + 1


def test_profitability_floor_narrow_spread_high_inv():
    """1c base gross + high inv: floor aggressively reduces skew.
    best_yes_bid=49, best_no_bid=50, sum=99, gross=1.
    inv=8, skew=4 without floor: yes=45, no=54, sum=99, gross=1.
    min_fees=2, need gross>=3. Floor can't achieve this (base gross=1).
    But it should still reduce skew to minimize damage."""
    yes_price, no_price = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=50,
        net_inventory=8, gamma=GAMMA, quote_offset=0)
    # Floor reduces skew to near-0 since base gross is only 1
    # Skew direction still preserved
    assert yes_price <= 49
    assert no_price >= 49


def test_profitability_floor_zero_inv_no_effect():
    """At zero inventory, no skew to reduce — floor is trivially a no-op."""
    yes_price, no_price = skewed_quotes(
        fair=49.0, best_yes_bid=48, best_no_bid=50,
        net_inventory=0, gamma=GAMMA, quote_offset=0)
    assert yes_price == 48
    assert no_price == 50
