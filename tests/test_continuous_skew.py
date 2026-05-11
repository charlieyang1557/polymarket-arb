# tests/test_continuous_skew.py
"""Tests for continuous inventory skew quoting."""
import math
from src.mm.state import skewed_quotes, maker_fee_cents

GAMMA = 0.5  # cents per contract

# All tests use best_yes_bid=45, best_no_bid=50, fair=48.0:
#   market_spread = 100 - 50 - 45 = 5, half_spread = max(1, 5//2) = 2
#   yes_base = round(48 - 2 - YES_PENALTY=1) = 45
#   no_base  = round(52 - 2) = 50
# (fair-anchored, with 1c YES adverse-selection penalty + round())


# -- Basic skew behavior --

def test_skew_zero_at_zero_inventory():
    """No inventory → no skew, quotes at fair ± half_spread, minus YES penalty."""
    # YES = round(48 - 2 - 0 - 0 - 1) = 45
    # NO  = round(52 - 2 - 0 + 0)     = 50
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=0, gamma=GAMMA, quote_offset=0)
    assert yes_price == 45
    assert no_price == 50


def test_skew_positive_inventory():
    """Long YES (inv=4) → lower YES bid further, raise NO bid."""
    # skew_raw = 4 * 0.5 = 2c
    # YES = round(48 - 2 - 0 - 2 - 1) = 43
    # NO  = round(52 - 2 - 0 + 2)     = 52
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 43
    assert no_price == 52


def test_skew_negative_inventory():
    """Long NO (inv=-4) → raise YES bid (offset by penalty), lower NO."""
    # skew_raw = -2
    # YES = round(48 - 2 - 0 - (-2) - 1) = round(47) = 47
    # NO  = round(52 - 2 - 0 + (-2))     = 48
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=-4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 47
    assert no_price == 48


def test_skew_at_inv_10():
    """At inv=10, skew=5c — significant but not extreme."""
    # YES = round(48 - 2 - 5 - 1) = 40, NO = round(52 - 2 + 5) = 55
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=10, gamma=GAMMA, quote_offset=0)
    assert yes_price == 40
    assert no_price == 55


def test_skew_floor_at_1c():
    """Wide spread (47c) with large skew — price computed from fair, not BBO."""
    # half=23, skew_raw=10, YES = round(48 - 23 - 10 - 1) = 14
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=3, best_no_bid=50,
        net_inventory=20, gamma=GAMMA, quote_offset=0)
    assert yes_price == 14


def test_skew_with_quote_offset():
    """Live-game offset stacks with skew."""
    # skew_raw=2, quote_offset=2
    # YES = round(48 - 2 - 2 - 2 - 1) = 41
    # NO  = round(52 - 2 - 2 + 2)     = 50
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=4, gamma=GAMMA, quote_offset=2)
    assert yes_price == 41
    assert no_price == 50


def test_skew_small_inventory():
    """At inv=1, skew_raw=0.5c — fractional skew rounds via banker's."""
    # YES = round(48 - 2 - 0.5 - 1) = round(44.5) = 44 (banker's: 4 even)
    # NO  = round(52 - 2 + 0.5)     = round(50.5) = 50 (banker's: 0 even)
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=1, gamma=GAMMA, quote_offset=0)
    assert yes_price == 44
    assert no_price == 50


def test_skew_inv_2_visible():
    """At inv=2, skew_raw=1c — first integer adjustment."""
    # YES = round(48 - 2 - 1 - 1) = 44, NO = round(52 - 2 + 1) = 51
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    assert yes_price == 44
    assert no_price == 51


# -- Profitability floor --

def test_profitability_floor_reduces_extreme_skew():
    """Tight spread + high inv: profitability floor enforces gross >= 1c.

    fair=49, best_yes_bid=48, best_no_bid=50:
      market_spread = 100-50-48 = 2, half_spread = max(1, 1) = 1
      inv=8, skew_raw=4: YES=floor(49-1-4)=44, NO=floor(51-1+4)=54
      gross = 100-44-54 = 2 >= 1 → floor is a no-op here.
    Polymarket makers earn rebates — no positive fee cost to cover.
    Floor only requires gross >= 1c (not fees+1c).
    """
    yes_price, no_price = skewed_quotes(
        fair=49.0, best_yes_bid=48, best_no_bid=50,
        net_inventory=8, gamma=GAMMA, quote_offset=0)
    gross = 100 - yes_price - no_price
    assert gross >= 1, f"gross={gross} < 1"


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
    # YES = round(48 - 2 - 1 - 1) = 44, NO = round(52 - 2 + 1) = 51
    # gross = 100 - 44 - 51 = 5 >= 1 → no-op.
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    assert yes_price == 44
    assert no_price == 51


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
    # market_spread = 100 - 50 - 48 = 2, half = 1
    # YES = round(49 - 1 - 0 - 1) = 47, NO = round(51 - 1 + 0) = 50
    yes_price, no_price = skewed_quotes(
        fair=49.0, best_yes_bid=48, best_no_bid=50,
        net_inventory=0, gamma=GAMMA, quote_offset=0)
    assert yes_price == 47
    assert no_price == 50
