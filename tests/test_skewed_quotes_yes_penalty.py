# tests/test_skewed_quotes_yes_penalty.py
"""Tests for skewed_quotes() following the YES-penalty revert (2026-05-20).

History: this module originally covered two fixes from
fill_asymmetry_diagnosis.md:

  Fix 1: 1c YES adverse-selection penalty added to skewed_quotes().
         REVERTED 2026-05-20 (YES_ADVERSE_SELECTION_PENALTY = 0).
         See simulator_recalibration_findings.md and
         trade_tape_aggressor_findings.md for the empirical justification.

  Fix 2: round() instead of math.floor() so YES and NO round symmetrically
         around fair. Floor always biased downward, which interacted with
         the YES-side adverse selection to amplify the imbalance ~10-15%.
         KEPT (independent of the penalty revert).

Tests below assert the post-revert behavior. The Fix 1 tests now confirm
NO penalty is applied; the Fix 2 tests still verify banker's rounding.
"""
from src.mm.state import skewed_quotes, YES_ADVERSE_SELECTION_PENALTY


# -- Sanity: constant is 0 ------------------------------------------------

def test_yes_adverse_selection_penalty_is_zero_post_revert():
    """The penalty constant is 0 (disabled). If a future change re-enables
    it, the rest of the tests in this module need re-checking."""
    assert YES_ADVERSE_SELECTION_PENALTY == 0


# -- Symmetric behavior at fair=50 ----------------------------------------

def test_yes_and_no_symmetric_at_fair_50_no_inventory():
    """At fair=50 with no inventory, YES and NO bids should be equal
    (penalty=0 means symmetric). Before revert: yes=48, no=49 (1c gap)."""
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    # Both: round(50 - 1 - 0 - 0 - 0) = 49
    assert yes_p == 49
    assert no_p == 49


def test_no_yes_penalty_across_fair_values():
    """YES bid should equal what we'd compute with penalty=0 at any fair."""
    # fair=52, half_spread=2 → yes = round(52-2-0-0-0) = 50
    #                          no  = round(48-2-0+0)   = 46
    yes_p, no_p = skewed_quotes(
        fair=52.0, best_yes_bid=48, best_no_bid=48,
        net_inventory=0, gamma=0.5)
    assert yes_p == 50  # was 49 with 1c penalty
    assert no_p == 46


def test_inventory_skew_still_applies_without_penalty():
    """Long YES (inv=4) should still skew YES down (less aggressive) and
    NO up (more aggressive) — the gamma * net_inventory mechanic is
    independent of the penalty."""
    # fair=50, half=1, inv=4, gamma=0.5 → skew_raw=2
    # YES = round(50-1-0-2-0) = round(47) = 47
    # NO  = round(50-1+2)     = round(51) = 51
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=4, gamma=0.5)
    assert yes_p == 47  # was 46 with 1c penalty
    assert no_p == 51


def test_no_bid_unchanged_by_penalty_revert():
    """NO bid was never penalized; revert is a no-op for the NO side."""
    _, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    # NO at fair=50, half=1, skew=0 → round(50-1+0) = 49
    assert no_p == 49


# -- Fix 2: round() instead of math.floor() -------------------------------
#
# These tests confirm banker's rounding still applies. Values updated to
# reflect penalty=0 (the YES side no longer has the extra -1 subtraction).

def test_round_at_half_cent_fair():
    """At fair=49.5, banker's rounding gives symmetric YES/NO."""
    # market_spread=100-49-49=2, half=1, inv=0
    # YES = round(49.5-1-0-0-0) = round(48.5) = 48 (banker's: 8 is even)
    # NO  = round(50.5-1+0)     = round(49.5) = 50 (banker's: 0 is even)
    yes_p, no_p = skewed_quotes(
        fair=49.5, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    assert yes_p == 48
    assert no_p == 50


def test_round_at_quarter_cent_below():
    """At fair=49.25 (frac=0.25), round-half-to-even gives both sides down."""
    # YES = round(49.25-1-0-0-0) = round(48.25) = 48
    # NO  = round(50.75-1+0)     = round(49.75) = 50
    yes_p, no_p = skewed_quotes(
        fair=49.25, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    assert yes_p == 48  # was 47 with 1c penalty
    assert no_p == 50


def test_round_at_quarter_cent_above():
    """At fair=49.75 (frac=0.75), should round UP both sides."""
    # YES = round(49.75-1-0-0-0) = round(48.75) = 49
    # NO  = round(50.25-1+0)     = round(49.25) = 49
    yes_p, no_p = skewed_quotes(
        fair=49.75, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    assert yes_p == 49  # was 48 with 1c penalty
    assert no_p == 49


def test_round_eliminates_floor_skew_at_high_fractional_fair():
    """Before Fix 2, at fair=49.6, floor() gave asymmetric results.
    With round(), both sides land at 49 — symmetric."""
    yes_p, no_p = skewed_quotes(
        fair=49.6, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    # YES = round(49.6-1-0-0-0) = round(48.6) = 49
    # NO  = round(50.4-1+0)     = round(49.4) = 49
    assert yes_p == 49  # was 48 with 1c penalty
    assert no_p == 49


# -- Profitability floor, quote_offset, and edge cases --------------------

def test_quote_offset_still_widens_both_sides():
    """quote_offset (live-game widening) stacks correctly without penalty."""
    # fair=50, half=1, quote_offset=2, no skew
    # YES = round(50-1-2-0-0) = round(47) = 47
    # NO  = round(50-1-2+0)   = round(47) = 47
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5, quote_offset=2)
    assert yes_p == 47  # was 46 with 1c penalty
    assert no_p == 47


def test_profitability_floor_preserved():
    """Profitability floor (gross >= 1c) still enforced after revert."""
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=20, gamma=1.0)
    assert 100 - yes_p - no_p >= 1
    assert yes_p >= 1
    assert no_p >= 1


def test_extreme_low_fair_floors_to_1c():
    """Even at very low fair, max(1, ...) clamp protects."""
    yes_p, _ = skewed_quotes(
        fair=2.0, best_yes_bid=1, best_no_bid=97,
        net_inventory=0, gamma=0.5)
    assert yes_p >= 1
