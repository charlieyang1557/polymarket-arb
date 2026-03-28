# tests/test_poly_daily_scan.py
"""Tests for Polymarket US daily scanner — pure functions only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_daily_scan import (
    poly_net_spread_cents,
    apply_prefilters,
    rank_candidates,
    avg_rank,
)


# --- poly_net_spread_cents ---

def test_net_spread_with_rebate():
    """Net spread = gross + maker rebate income (rebate is positive addition)."""
    # spread=5c, midpoint=50c
    # Kalshi: net = 5 - 2*ceil(0.0175*0.5*0.5*100) = 5 - 2*1 = 3c
    # Polymarket: rebate per side = 0.25 * 0.02 * 0.5*0.5 * 100 = 0.125c
    # net = 5 + 2*0.125 = 5.25c
    result = poly_net_spread_cents(5, 50)
    assert result > 5.0  # rebate adds to net spread
    assert abs(result - 5.25) < 0.01


def test_net_spread_zero_spread():
    assert poly_net_spread_cents(0, 50) == 0


def test_net_spread_at_extreme():
    """At midpoint 90c, P*(1-P) is small → small rebate."""
    result = poly_net_spread_cents(5, 90)
    # rebate = 2 * 0.25 * 0.02 * 0.9 * 0.1 * 100 = 0.09c
    assert abs(result - 5.09) < 0.01


# --- apply_prefilters ---

def _candidate(spread=5, midpoint=50, yes_depth=200, no_depth=200,
               symmetry=1.0, net_spread=5.25, best_yes_depth=50,
               best_no_depth=50):
    return {
        "slug": "test-market",
        "spread": spread,
        "midpoint": midpoint,
        "yes_depth": yes_depth,
        "no_depth": no_depth,
        "symmetry": symmetry,
        "net_spread": net_spread,
        "best_yes_depth": best_yes_depth,
        "best_no_depth": best_no_depth,
    }


def test_prefilter_passes_good_candidate():
    c = _candidate()
    result = apply_prefilters(c)
    assert result is True


def test_prefilter_passes_1c_spread():
    """1c spread is profitable on Polymarket (maker rebates)."""
    c = _candidate(spread=1, net_spread=1.25)
    assert apply_prefilters(c) is True


def test_prefilter_fails_zero_spread():
    c = _candidate(spread=0)
    assert apply_prefilters(c) is False


def test_prefilter_fails_wide_spread():
    c = _candidate(spread=12)
    assert apply_prefilters(c) is False


def test_prefilter_fails_extreme_midpoint_low():
    c = _candidate(midpoint=15)
    assert apply_prefilters(c) is False


def test_prefilter_fails_extreme_midpoint_high():
    c = _candidate(midpoint=85)
    assert apply_prefilters(c) is False


def test_prefilter_fails_no_depth():
    c = _candidate(best_yes_depth=0)
    assert apply_prefilters(c) is False


def test_prefilter_fails_asymmetric():
    c = _candidate(symmetry=0.1)
    assert apply_prefilters(c) is False


def test_prefilter_fails_negative_net_spread():
    c = _candidate(net_spread=-1)
    assert apply_prefilters(c) is False


# --- avg_rank ---

def test_avg_rank_ascending():
    """Ascending: lowest value gets rank 1."""
    ranks = avg_rank([30, 10, 20], ascending=True)
    assert ranks[0] == 3.0  # 30 = rank 3
    assert ranks[1] == 1.0  # 10 = rank 1
    assert ranks[2] == 2.0  # 20 = rank 2


def test_avg_rank_descending():
    """Descending: highest value gets rank 1."""
    ranks = avg_rank([30, 10, 20], ascending=False)
    assert ranks[0] == 1.0  # 30 = rank 1
    assert ranks[1] == 3.0  # 10 = rank 3
    assert ranks[2] == 2.0  # 20 = rank 2


def test_avg_rank_ties():
    """Tied values get average rank."""
    ranks = avg_rank([10, 10, 20], ascending=True)
    assert ranks[0] == 1.5  # tied for rank 1-2 → avg 1.5
    assert ranks[1] == 1.5
    assert ranks[2] == 3.0


# --- rank_candidates ---

def test_rank_basic():
    """Two-metric ranking: net_spread (desc) + binding_queue (asc)."""
    candidates = [
        {"passes": True, "net_spread": 5, "binding_queue": 100,
         "slug": "a"},
        {"passes": True, "net_spread": 3, "binding_queue": 50,
         "slug": "b"},
        {"passes": True, "net_spread": 8, "binding_queue": 200,
         "slug": "c"},
    ]
    ranked = rank_candidates(candidates)
    passing = [c for c in ranked if c["passes"]]
    # Each has a composite_rank
    assert all("composite_rank" in c for c in passing)
    # Sorted by composite (lowest first)
    composites = [c["composite_rank"] for c in passing]
    assert composites == sorted(composites)


def test_rank_failing_excluded():
    """Failing candidates go to the end, unranked."""
    candidates = [
        {"passes": True, "net_spread": 5, "binding_queue": 100,
         "slug": "a"},
        {"passes": False, "slug": "b"},
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0]["slug"] == "a"
    assert ranked[1]["slug"] == "b"
    assert "composite_rank" not in ranked[1]


def test_rank_empty():
    assert rank_candidates([]) == []
