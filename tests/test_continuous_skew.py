# tests/test_continuous_skew.py
"""Tests for continuous inventory skew quoting."""
from src.mm.state import skewed_quotes

GAMMA = 0.5  # cents per contract


# -- Basic skew behavior --

def test_skew_zero_at_zero_inventory():
    """No inventory → no skew, quotes symmetric around fair price."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=0, gamma=GAMMA, quote_offset=0)
    # No skew: quote at best bid on each side
    assert yes_price == 47
    assert no_price == 53


def test_skew_positive_inventory():
    """Long YES (inv=4) → lower YES bid, raise NO bid to attract NO fills.

    skew = 4 * 0.5 = 2c
    YES bid: best_bid - skew = 47 - 2 = 45
    NO bid:  best_bid + skew = 53 + 2 = 55
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 45  # less aggressive on YES
    assert no_price == 55   # more aggressive on NO


def test_skew_negative_inventory():
    """Long NO (inv=-4) → raise YES bid, lower NO bid to attract YES fills.

    skew = -4 * 0.5 = -2c
    YES bid: best_bid - skew = 47 - (-2) = 49
    NO bid:  best_bid + skew = 53 + (-2) = 51
    """
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=-4, gamma=GAMMA, quote_offset=0)
    assert yes_price == 49  # more aggressive on YES
    assert no_price == 51   # less aggressive on NO


def test_skew_at_inv_10():
    """At inv=10, skew=5c — significant but not extreme."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=10, gamma=GAMMA, quote_offset=0)
    # skew = 10 * 0.5 = 5c
    assert yes_price == 42  # 47 - 5
    assert no_price == 58   # 53 + 5


def test_skew_floor_at_1c():
    """Skew can't push price below 1c."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=3, best_no_bid=53,
        net_inventory=20, gamma=GAMMA, quote_offset=0)
    # skew = 10c, 3 - 10 = -7 → clamp to 1
    assert yes_price == 1


def test_skew_with_quote_offset():
    """Live-game offset stacks with skew."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=4, gamma=GAMMA, quote_offset=2)
    # offset=2: base = best_bid - 2
    # skew=2: yes = 45 - 2 = 43, no = 51 + 2 = 53
    assert yes_price == 43  # 47 - 2(offset) - 2(skew)
    assert no_price == 53   # 53 - 2(offset) + 2(skew)


def test_skew_small_inventory():
    """At inv=1, skew=0.5c → rounds to 0, no visible effect."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=1, gamma=GAMMA, quote_offset=0)
    # skew = 0.5c → int(0.5) = 0 (truncates, not rounds)
    # This is fine — sub-cent skew doesn't affect integer prices
    assert yes_price == 47
    assert no_price == 53


def test_skew_inv_2_visible():
    """At inv=2, skew=1c — first visible adjustment."""
    yes_price, no_price = skewed_quotes(
        fair=48.0, best_yes_bid=47, best_no_bid=53,
        net_inventory=2, gamma=GAMMA, quote_offset=0)
    # skew = 1c
    assert yes_price == 46  # 47 - 1
    assert no_price == 54   # 53 + 1
