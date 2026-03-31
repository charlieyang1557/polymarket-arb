# tests/test_capital_risk.py
"""Tests for capital-aware risk thresholds."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_paper_mm import compute_risk_params


def test_capital_25():
    """$25 capital → MAX_INV=10, UNHEDGED=5, AGGRESS=8."""
    p = compute_risk_params(2500)
    assert p["max_inventory"] == 10
    assert p["max_unhedged_exit"] == 5
    assert p["aggress_threshold"] == 8


def test_capital_200():
    """$200 capital → MAX_INV=80, UNHEDGED=40, AGGRESS=64."""
    p = compute_risk_params(20000)
    assert p["max_inventory"] == 80
    assert p["max_unhedged_exit"] == 40
    assert p["aggress_threshold"] == 64


def test_capital_tiny():
    """$5 capital → floors at minimums."""
    p = compute_risk_params(500)
    assert p["max_inventory"] == 4   # floor
    assert p["max_unhedged_exit"] == 2  # floor
    assert p["aggress_threshold"] == max(2, int(4 * 0.8))


def test_soft_close_no_flatten_under_threshold():
    """inv=3, UNHEDGED=5 → no flattening needed."""
    from scripts.poly_paper_mm import should_soft_close_flatten
    assert should_soft_close_flatten(3, 5) is False
    assert should_soft_close_flatten(-3, 5) is False


def test_soft_close_flatten_over_threshold():
    """inv=8, UNHEDGED=5 → flatten to 5."""
    from scripts.poly_paper_mm import should_soft_close_flatten
    assert should_soft_close_flatten(8, 5) is True
    assert should_soft_close_flatten(-8, 5) is True


def test_soft_close_exact_threshold():
    """inv exactly at UNHEDGED → no flatten."""
    from scripts.poly_paper_mm import should_soft_close_flatten
    assert should_soft_close_flatten(5, 5) is False
