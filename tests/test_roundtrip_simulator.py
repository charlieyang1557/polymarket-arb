"""Tests for the round-trip simulator.

The simulator is a counterfactual that, given the 326 historical live fills,
estimates the bot's REALIZED round-trip P&L (not hold-to-settle) under
different YES-penalty survival models. This addresses the Step 2 limitation
of the hold-to-settle counterfactual (which over-estimates losses).

Validation approach:
- Baseline (no penalty applied) should produce a P&L close to the bot's
  realized -$2.92.
- Counterfactual under each survival model gives a predicted post-penalty
  P&L.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.research.roundtrip_simulator import (
    apply_survival_to_fills,
    simulate_session_ticker,
    SURVIVAL_MODELS,
)


# ---------- pair-off mechanics --------------------------------------------

def test_one_yes_one_no_pairs_off_cleanly():
    """1 yes_bid + 1 no_bid → 1 pair (gross = 100 - yes_cost - no_cost), no remainder."""
    fills = [
        {"id": 1, "side": "yes_bid", "price": 48, "size": 1,
         "filled_at": "2026-04-01T10:00:00+00:00"},
        {"id": 2, "side": "no_bid", "price": 50, "size": 1,
         "filled_at": "2026-04-01T10:01:00+00:00"},
    ]
    result = simulate_session_ticker(fills, last_snap=None, outcome=None)
    assert result["pair_pnl_c"] == 2  # 100 - 48 - 50
    assert result["yes_remaining"] == 0
    assert result["no_remaining"] == 0
    assert result["exit_pnl_c"] == 0


def test_two_yes_one_no_leaves_one_yes_unpaired():
    """2 yes_bid + 1 no_bid → 1 pair (FIFO yes first) + 1 yes remaining."""
    fills = [
        {"id": 1, "side": "yes_bid", "price": 48, "size": 1, "filled_at": "t0"},
        {"id": 2, "side": "yes_bid", "price": 47, "size": 1, "filled_at": "t1"},
        {"id": 3, "side": "no_bid", "price": 50, "size": 1, "filled_at": "t2"},
    ]
    snap = {"best_yes_bid": 46, "yes_ask": 48}
    result = simulate_session_ticker(fills, last_snap=snap, outcome=None)
    # FIFO: yes@48 pairs with no@50 → gross = 100 - 48 - 50 = 2c
    assert result["pair_pnl_c"] == 2
    # Remaining yes@47, exit by selling YES @ best_yes_bid=46 → -1c
    assert result["yes_remaining"] == 1
    assert result["exit_pnl_c"] == -1


def test_one_no_unpaired_exits_at_no_bid():
    """Unpaired NO inventory exits by selling NO at (100 - yes_ask)."""
    fills = [{"id": 1, "side": "no_bid", "price": 50, "size": 1, "filled_at": "t0"}]
    snap = {"best_yes_bid": 46, "yes_ask": 48}  # best_no_bid = 100-48 = 52
    result = simulate_session_ticker(fills, last_snap=snap, outcome=None)
    # Exit P&L: sell NO at 52 → 52 - 50 = +2c
    assert result["exit_pnl_c"] == 2


def test_size_scales_pair_pnl():
    """Larger size scales per-contract P&L proportionally."""
    fills = [
        {"id": 1, "side": "yes_bid", "price": 48, "size": 3, "filled_at": "t0"},
        {"id": 2, "side": "no_bid", "price": 50, "size": 3, "filled_at": "t1"},
    ]
    result = simulate_session_ticker(fills, last_snap=None, outcome=None)
    assert result["pair_pnl_c"] == 6  # 2c × 3


def test_unequal_sizes_partial_pair():
    """yes_size=3 + no_size=1 → 1 contract paired, 2 yes remaining."""
    fills = [
        {"id": 1, "side": "yes_bid", "price": 48, "size": 3, "filled_at": "t0"},
        {"id": 2, "side": "no_bid", "price": 50, "size": 1, "filled_at": "t1"},
    ]
    snap = {"best_yes_bid": 46, "yes_ask": 48}
    result = simulate_session_ticker(fills, last_snap=snap, outcome=None)
    assert result["pair_pnl_c"] == 2  # 1 paired pair @ 2c
    assert result["yes_remaining"] == 2  # 2 yes contracts left
    # Exit each at 46 → loss of 2c each → -4c total
    assert result["exit_pnl_c"] == -4


# ---------- settlement fallback when snapshot missing --------------------

def test_unpaired_yes_falls_back_to_settlement_when_no_snapshot():
    """If snapshot missing, unpaired YES inventory holds to settlement."""
    fills = [{"id": 1, "side": "yes_bid", "price": 30, "size": 1, "filled_at": "t0"}]
    # YES + outcome YES → +70c
    result_yes = simulate_session_ticker(fills, last_snap=None, outcome="yes")
    assert result_yes["exit_pnl_c"] == 70
    # YES + outcome NO → -30c
    result_no = simulate_session_ticker(fills, last_snap=None, outcome="no")
    assert result_no["exit_pnl_c"] == -30


def test_unpaired_no_settles_correctly():
    """Symmetric case for unpaired NO."""
    fills = [{"id": 1, "side": "no_bid", "price": 40, "size": 1, "filled_at": "t0"}]
    # NO + outcome NO → +60c
    result_no = simulate_session_ticker(fills, last_snap=None, outcome="no")
    assert result_no["exit_pnl_c"] == 60
    # NO + outcome YES → -40c
    result_yes = simulate_session_ticker(fills, last_snap=None, outcome="yes")
    assert result_yes["exit_pnl_c"] == -40


# ---------- fee accounting ------------------------------------------------

def test_maker_rebate_applied_per_fill():
    """Each fill earns negative fee (rebate) under sports config."""
    fills = [
        {"id": 1, "side": "yes_bid", "price": 50, "size": 1, "filled_at": "t0"},
        {"id": 2, "side": "no_bid", "price": 50, "size": 1, "filled_at": "t1"},
    ]
    result = simulate_session_ticker(fills, last_snap=None, outcome=None)
    # Maker rebate at P=0.5: -0.25 * 0.02 * 0.25 * 100 = -0.125c per contract
    # 2 contracts → -0.25c total (rebate, negative = credit)
    assert result["maker_fee_c"] < 0
    assert abs(result["maker_fee_c"] - (-0.25)) < 0.01


def test_taker_fee_applied_on_unpaired_exits():
    """Taker fee is charged on exit-side aggression to flatten unpaired inventory."""
    fills = [{"id": 1, "side": "yes_bid", "price": 50, "size": 1, "filled_at": "t0"}]
    snap = {"best_yes_bid": 48, "yes_ask": 52}
    result = simulate_session_ticker(fills, last_snap=snap, outcome=None)
    # Exit by selling YES @ best_yes_bid=48 → P&L = 48 - 50 = -2c
    # Taker fee at exit P=0.48: 0.02 * 0.48 * 0.52 * 100 = 0.4992c ≈ 0.5c
    assert result["exit_pnl_c"] == -2
    assert result["taker_fee_c"] > 0
    assert abs(result["taker_fee_c"] - 0.4992) < 0.01


def test_no_taker_fee_when_settles_to_outcome():
    """Settlement (no aggression) does not incur taker fee."""
    fills = [{"id": 1, "side": "yes_bid", "price": 30, "size": 1, "filled_at": "t0"}]
    result = simulate_session_ticker(fills, last_snap=None, outcome="yes")
    assert result["taker_fee_c"] == 0


def test_net_pnl_combines_gross_and_fees():
    """net_pnl_c = pair_pnl + exit_pnl - maker_fee - taker_fee."""
    fills = [
        {"id": 1, "side": "yes_bid", "price": 50, "size": 1, "filled_at": "t0"},
        {"id": 2, "side": "no_bid", "price": 50, "size": 1, "filled_at": "t1"},
    ]
    result = simulate_session_ticker(fills, last_snap=None, outcome=None)
    expected_net = (result["pair_pnl_c"] + result["exit_pnl_c"]
                    - result["maker_fee_c"] - result["taker_fee_c"])
    assert abs(result["net_pnl_c"] - expected_net) < 0.01


# ---------- survival probability filtering -------------------------------

def test_apply_survival_p0_drops_all_yes_bid():
    """survival_fn returning 0.0 drops every yes_bid fill."""
    fills = [
        {"id": 1, "side": "yes_bid", "_bucket": 0, "filled_at": "t0"},
        {"id": 2, "side": "no_bid", "_bucket": 0, "filled_at": "t1"},
        {"id": 3, "side": "yes_bid", "_bucket": -1, "filled_at": "t2"},
    ]
    survival_fn = lambda f: 0.0
    kept = apply_survival_to_fills(fills, survival_fn, random.Random(0))
    assert len(kept) == 1
    assert kept[0]["side"] == "no_bid"


def test_apply_survival_p1_keeps_all():
    fills = [
        {"id": 1, "side": "yes_bid", "_bucket": 0, "filled_at": "t0"},
        {"id": 2, "side": "no_bid", "_bucket": 0, "filled_at": "t1"},
    ]
    kept = apply_survival_to_fills(fills, lambda f: 1.0, random.Random(0))
    assert len(kept) == 2


def test_apply_survival_does_not_affect_no_bid():
    """The penalty is YES-only; no_bid fills survive regardless of survival_fn."""
    fills = [{"id": 1, "side": "no_bid", "_bucket": 0, "filled_at": "t0"}]
    kept = apply_survival_to_fills(fills, lambda f: 0.0, random.Random(0))
    assert len(kept) == 1


def test_survival_uses_bucket_lookup():
    """Bucket-specific survival rates are honored."""
    fills = [
        {"id": 1, "side": "yes_bid", "_bucket": 0, "filled_at": "t0"},
        {"id": 2, "side": "yes_bid", "_bucket": -1, "filled_at": "t1"},
        {"id": 3, "side": "yes_bid", "_bucket": "no_snapshot", "filled_at": "t2"},
    ]
    # bucket=0 → 1.0 (keep), bucket=-1 → 0.0 (drop), no_snapshot → 1.0 (keep)
    def survival_fn(f):
        return {0: 1.0, -1: 0.0, "no_snapshot": 1.0}.get(f["_bucket"], 0.5)
    kept = apply_survival_to_fills(fills, survival_fn, random.Random(0))
    kept_ids = sorted(f["id"] for f in kept)
    assert kept_ids == [1, 3]


def test_survival_reproducible_with_seed():
    fills = [{"id": i, "side": "yes_bid", "_bucket": 0, "filled_at": f"t{i}"}
             for i in range(20)]
    kept_a = apply_survival_to_fills(fills, lambda f: 0.5, random.Random(42))
    kept_b = apply_survival_to_fills(fills, lambda f: 0.5, random.Random(42))
    assert [f["id"] for f in kept_a] == [f["id"] for f in kept_b]


# ---------- shipped survival model values --------------------------------

def test_survival_models_have_three_named_levels():
    """The three survival models from the static counterfactual ship intact."""
    assert set(SURVIVAL_MODELS.keys()) == {"pessimistic", "base", "optimistic"}
    for name in SURVIVAL_MODELS:
        m = SURVIVAL_MODELS[name]
        # all buckets present
        assert 0 in m and -1 in m and "+1_or_above" in m and "no_snapshot" in m
        for key, p in m.items():
            assert 0 <= p <= 1, f"{name}[{key}] = {p}"


# ---------- chronological ordering ---------------------------------------

def test_fills_processed_in_chronological_order_not_insertion_order():
    """Simulator should sort by filled_at, not rely on input order."""
    # Two fills out-of-order in input
    fills = [
        {"id": 2, "side": "no_bid", "price": 50, "size": 1,
         "filled_at": "2026-04-01T10:01:00+00:00"},
        {"id": 1, "side": "yes_bid", "price": 48, "size": 1,
         "filled_at": "2026-04-01T10:00:00+00:00"},
    ]
    result = simulate_session_ticker(fills, last_snap=None, outcome=None)
    # YES@48 should pair with NO@50 regardless of input order
    assert result["pair_pnl_c"] == 2
    assert result["yes_remaining"] == 0
    assert result["no_remaining"] == 0
