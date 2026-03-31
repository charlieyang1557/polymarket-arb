# tests/test_scaling.py
"""Tests for full capital-aware scaling: order size, normalized skew."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_paper_mm import compute_risk_params
from src.mm.state import skewed_quotes


# --- Auto order size ---

def test_scaling_200_order_size():
    """$200 → MAX_INV=80 → SIZE=16."""
    p = compute_risk_params(20000)
    assert p["order_size"] == 16


def test_scaling_25_order_size():
    """$25 → MAX_INV=10 → SIZE=2."""
    p = compute_risk_params(2500)
    assert p["order_size"] == 2


def test_scaling_5_order_size():
    """$5 → MAX_INV=4 → SIZE=1 (floor)."""
    p = compute_risk_params(500)
    assert p["order_size"] == 1


# --- Normalized skew ---

def test_skew_200_at_half_inv():
    """$200, inv=40 (half of MAX_INV=80): skew = (40/80)*5 = 2.5c."""
    # gamma = MAX_SKEW / MAX_INV = 5/80 = 0.0625
    gamma = 5.0 / 80
    yes, no = skewed_quotes(
        fair=50, best_yes_bid=48, best_no_bid=48,
        net_inventory=40, gamma=gamma, quote_offset=0)
    # skew = 40 * 0.0625 = 2.5
    # yes = floor(48 - 2.5) = 45
    # no = floor(48 + 2.5) = 50
    assert yes == 45
    assert no == 50


def test_skew_25_at_half_inv():
    """$25, inv=5 (half of MAX_INV=10): skew = (5/10)*5 = 2.5c — same as $200."""
    gamma = 5.0 / 10  # = 0.5 (current value!)
    yes, no = skewed_quotes(
        fair=50, best_yes_bid=48, best_no_bid=48,
        net_inventory=5, gamma=gamma, quote_offset=0)
    # skew = 5 * 0.5 = 2.5
    assert yes == 45
    assert no == 50


def test_skew_25_matches_current():
    """At $25, normalized gamma = 0.5 — identical to current hardcoded gamma."""
    p = compute_risk_params(2500)
    gamma = p["gamma"]
    assert abs(gamma - 0.5) < 0.001


def test_skew_200_gamma():
    """$200 gamma = 5/80 = 0.0625."""
    p = compute_risk_params(20000)
    assert abs(p["gamma"] - 0.0625) < 0.001


# --- Full param check ---

def test_full_params_200():
    p = compute_risk_params(20000)
    assert p["max_inventory"] == 80
    assert p["order_size"] == 16
    assert p["aggress_threshold"] == 64
    assert p["max_unhedged_exit"] == 40
    assert abs(p["gamma"] - 5.0 / 80) < 0.001


def test_full_params_25():
    """$25 produces identical behavior to current main branch."""
    p = compute_risk_params(2500)
    assert p["max_inventory"] == 10
    assert p["order_size"] == 2
    assert p["aggress_threshold"] == 8
    assert p["max_unhedged_exit"] == 5
    assert abs(p["gamma"] - 0.5) < 0.001
