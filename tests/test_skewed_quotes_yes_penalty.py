# tests/test_skewed_quotes_yes_penalty.py
"""Asymmetric-quote fixes from fill_asymmetry_diagnosis.md.

Production data (May 2026) showed 209 yes_bid vs 117 no_bid fills with
99/14 first-fill ratio at inv=0 — confirming structural adverse selection
from YES-seller taker flow on Polymarket sports. This module covers the
two state.py-level remediations:

  Fix 1: 1c YES adverse-selection penalty added to skewed_quotes(),
         lowering the YES bid by 1c on every quote. Cuts yes_bid fill
         rate ~30-50% per simulation.

  Fix 2: round() instead of math.floor() so YES and NO round symmetrically
         around fair. Floor always biased downward, which interacted with
         the YES-side adverse selection to amplify the imbalance ~10-15%.

Reference: data/research/fill_asymmetry_diagnosis.md "Recommended fixes"
"""
from src.mm.state import skewed_quotes


# -- Fix 1: 1c YES adverse-selection penalty -------------------------------

def test_yes_bid_lower_than_symmetric_reference():
    """At symmetric inputs (fair=50, balanced spread, no inv), YES bid
    should be 1c LOWER than NO bid because of the adverse-selection
    penalty. Before the fix YES=NO=49; after, YES=48."""
    # market_spread=100-49-49=2, half=1
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    # Symmetric (no penalty): both would be round(50-1-0-0) = 49
    # With penalty: yes = round(50-1-0-0-1) = 48; no unchanged.
    assert yes_p == 48
    assert no_p == 49


def test_yes_penalty_applies_across_fair_values():
    """The 1c YES penalty should apply at any fair value, not just 50."""
    # fair=52, half_spread=2 → yes_base = round(52-2-0-0) = 50,
    #                          no_base = round(48-2+0) = 46
    # With penalty: yes = round(52-2-0-0-1) = 49, no unchanged
    yes_p, no_p = skewed_quotes(
        fair=52.0, best_yes_bid=48, best_no_bid=48,
        net_inventory=0, gamma=0.5)
    assert yes_p == 49  # was 50 without penalty
    assert no_p == 46


def test_yes_penalty_stacks_with_inventory_skew():
    """Penalty stacks with skew: long YES skews YES bid down further,
    then penalty drops another 1c on top."""
    # fair=50, half=1, inv=4, gamma=0.5 → skew_raw=2
    # YES = round(50-1-0-2-1) = round(46) = 46
    # NO  = round(50-1+2)     = round(51) = 51
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=4, gamma=0.5)
    assert yes_p == 46
    assert no_p == 51


def test_yes_penalty_does_not_apply_to_no_bid():
    """NO bid should not be penalized — flow asymmetry favors NO-side."""
    # At inv=0, fair=50, the NO bid should be the un-penalized value.
    _, no_p_with_penalty = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    # NO at fair=50, half=1, skew=0 → round(50-1+0) = 49
    assert no_p_with_penalty == 49


# -- Fix 2: round() instead of math.floor() --------------------------------

def test_round_at_half_cent_fair():
    """At fair=49.5, banker's rounding ensures symmetric behavior."""
    # market_spread=100-49-49=2, half=1, inv=0
    # YES = round(49.5-1-0-0-1) = round(47.5) = 48 (banker's: 8 even)
    # NO  = round(50.5-1+0)     = round(49.5) = 50 (banker's: 0 even)
    yes_p, no_p = skewed_quotes(
        fair=49.5, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    assert yes_p == 48
    assert no_p == 50


def test_round_at_quarter_cent_below():
    """At fair=49.25 (frac=0.25), should round DOWN both sides."""
    # YES = round(49.25-1-0-0-1) = round(47.25) = 47
    # NO  = round(50.75-1+0)     = round(49.75) = 50
    yes_p, no_p = skewed_quotes(
        fair=49.25, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    assert yes_p == 47
    assert no_p == 50


def test_round_at_quarter_cent_above():
    """At fair=49.75 (frac=0.75), should round UP both sides."""
    # YES = round(49.75-1-0-0-1) = round(47.75) = 48
    # NO  = round(50.25-1+0)     = round(49.25) = 49
    yes_p, no_p = skewed_quotes(
        fair=49.75, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    assert yes_p == 48
    assert no_p == 49


def test_round_eliminates_floor_skew_at_high_fractional_fair():
    """Before fix, at fair=49.6, floor() gave yes=47, no=49 (asymmetric).
    With round(), yes=48, no=49 — symmetric within YES adjustment."""
    yes_p, no_p = skewed_quotes(
        fair=49.6, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5)
    # round(47.6)=48 (vs floor: 47)
    # round(49.4)=49 (vs floor: 49)
    assert yes_p == 48
    assert no_p == 49


# -- Combined: Fix 1 + Fix 2 interacting with skew + quote_offset ----------

def test_yes_penalty_with_quote_offset():
    """quote_offset (live-game widening) stacks with YES penalty."""
    # fair=50, half=1, quote_offset=2, no skew
    # YES = round(50-1-2-0-1) = round(46) = 46
    # NO  = round(50-1-2+0)   = round(47) = 47
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=0, gamma=0.5, quote_offset=2)
    assert yes_p == 46
    assert no_p == 47


def test_yes_penalty_preserves_profitability_floor():
    """Profitability floor (gross >= 1c) still enforced after fixes."""
    # Wide skew that would push gross below 1 still gets clamped.
    yes_p, no_p = skewed_quotes(
        fair=50.0, best_yes_bid=49, best_no_bid=49,
        net_inventory=20, gamma=1.0)
    assert 100 - yes_p - no_p >= 1
    assert yes_p >= 1
    assert no_p >= 1


def test_yes_penalty_at_extreme_low_fair_floors_to_1c():
    """If fair is so low the YES penalty would push YES below 1c,
    the max(1, ...) floor still protects."""
    # fair=2, half=1, penalty=1 → would be 0. Floor clamps to 1.
    yes_p, _ = skewed_quotes(
        fair=2.0, best_yes_bid=1, best_no_bid=97,
        net_inventory=0, gamma=0.5)
    assert yes_p >= 1
