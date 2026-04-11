# tests/test_continuous_skew.py
"""Tests for continuous inventory skew quoting."""
import math
from src.mm.state import skewed_quotes, maker_fee_cents

GAMMA = 0.5  # cents per contract

# All tests use best_yes_bid=45, best_no_bid=50, fair=48.0:
#   market_spread = 100 - 50 - 45 = 5, half_spread = max(1, 5//2) = 2
#   yes_base = floor(48 - 2) = 46, no_base = floor(52 - 2) = 50
# (fair-anchored, not BBO-anchored)


# -- Basic skew behavior --

def test_skew_zero_at_zero_inventory():
    """No inventory → no skew, quotes at fair ± half_spread.

    fair=48, half_spread=2: YES=floor(48-2)=46, NO=floor(52-2)=50.
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=0, gamma=GAMMA, quote_offset=0)
    assert yes_price == 46
    assert no_price == 50


def test_skew_positive_inventory():
    """Long YES (inv=4) → lower YES bid, raise NO bid to attract NO fills.

    skew_raw = 4 * 0.5 = 2c
    YES bid: floor(48 - 2 - 2) = 44
    NO bid:  floor(52 - 2 + 2) = 52
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 44
    assert no_price == 52


def test_skew_negative_inventory():
    """Long NO (inv=-4) → raise YES bid, lower NO bid to attract YES fills.

    skew_raw = -4 * 0.5 = -2c
    YES bid: floor(48 - 2 - (-2)) = 48
    NO bid:  floor(52 - 2 + (-2)) = 48
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=-4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 48
    assert no_price == 48


def test_skew_at_inv_10():
    """At inv=10, skew=5c — significant but not extreme.

    YES: floor(48-2-5)=41, NO: floor(52-2+5)=55, gross=4 → profitable, floor doesn't trigger.
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=10, gamma=GAMMA, quote_offset=0)
    assert yes_price == 41  # floor(48 - 2 - 5)
    assert no_price == 55   # floor(52 - 2 + 5)


def test_skew_floor_at_1c():
    """Wide spread (47c) with large skew — price computed from fair, not BBO.

    best_yes_bid=3, best_no_bid=50, fair=48:
      market_spread = 100-50-3 = 47, half_spread = max(1, 23) = 23
      skew_raw = 20*0.5 = 10
      YES: max(1, floor(48-23-10)) = max(1, 15) = 15
    The max(1,...) floor is still enforced but fair-anchoring means we're
    at 15c, not 1c.
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=3, best_no_bid=50,
        net_inventory=20, gamma=GAMMA, quote_offset=0)
    assert yes_price == 15


def test_skew_with_quote_offset():
    """Live-game offset stacks with skew.

    skew_raw=2, quote_offset=2:
      YES: floor(48 - 2 - 2 - 2) = 42
      NO:  floor(52 - 2 - 2 + 2) = 50
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=4, gamma=GAMMA, quote_offset=2)
    assert yes_price == 42
    assert no_price == 50


def test_skew_small_inventory():
    """At inv=1, skew_raw=0.5c → fractional floor applies.

    YES: floor(48 - 2 - 0.5) = floor(45.5) = 45
    NO:  floor(52 - 2 + 0.5) = floor(50.5) = 50
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=1, gamma=GAMMA, quote_offset=0)
    assert yes_price == 45
    assert no_price == 50


def test_skew_inv_2_visible():
    """At inv=2, skew_raw=1c — first integer adjustment.

    YES: floor(48 - 2 - 1) = 45
    NO:  floor(52 - 2 + 1) = 51
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    assert yes_price == 45  # floor(48 - 2 - 1)
    assert no_price == 51   # floor(52 - 2 + 1)


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
    """Wide spread + small skew = already profitable, floor is no-op.

    fair=48, market_spread=5, half_spread=2, inv=2, skew_raw=1:
      YES: floor(48-2-1) = 45
      NO:  floor(52-2+1) = 51
      gross = 100-45-51 = 4 >= 1 → floor is a no-op.
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=45, best_no_bid=50,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    assert yes_price == 45  # floor(48 - 2 - 1)
    assert no_price == 51   # floor(52 - 2 + 1)


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
