"""Tests for the taker counterfactual analysis.

The script in scripts/research/taker_counterfactual.py computes:
  For each historical maker fill, flip to a taker on the OPPOSITE side
  (joining the historical taker's direction instead of resting as a maker
  on the side that was being adversely selected).

If maker yes_bid fill happened at price P (bot acquired YES at P), the
taker flip is: bot SELLS YES at price P (= joins the YES-seller taker
who hit the maker). Bot is now short YES at P → settlement:
  - If YES wins (settled=1): bot owes 100, received P → P&L = P - 100
  - If NO wins  (settled=0): bot owes 0,  received P → P&L = +P

Plus taker fee (positive cost) instead of maker rebate (negative cost).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from scripts.research.taker_counterfactual import (
    flip_fill_to_taker_pnl_settled,
    taker_fee_cents,
    aggregate_per_prefix,
)


# ---------- taker fee --------------------------------------------------

def test_taker_fee_at_mid_50():
    """Polymarket sports taker fee: 0.02 * P * (1-P) * 100, max at P=0.5."""
    # at P=0.5: 0.02 * 0.5 * 0.5 * 100 = 0.5c per contract
    assert abs(taker_fee_cents(50, 1) - 0.5) < 0.01


def test_taker_fee_at_extreme():
    """At extreme prices (P=0.1 or P=0.9), fee is much smaller."""
    # at P=0.1: 0.02 * 0.1 * 0.9 * 100 = 0.18c
    assert abs(taker_fee_cents(10, 1) - 0.18) < 0.01
    assert abs(taker_fee_cents(90, 1) - 0.18) < 0.01


def test_taker_fee_scales_with_size():
    """Larger size → proportionally larger fee."""
    one = taker_fee_cents(50, 1)
    ten = taker_fee_cents(50, 10)
    assert abs(ten - 10 * one) < 0.01


# ---------- flip yes_bid maker → yes-seller taker (settles) ------------

def test_yes_bid_flip_yes_wins():
    """Maker yes_bid at 48, YES wins.
    Maker P&L: long YES at 48 → +52 per contract (before fees).
    Taker flip: sold YES at 48 → owes 100 at settle → -52 per contract."""
    fill = {"side": "yes_bid", "price": 48, "size": 1}
    pnl = flip_fill_to_taker_pnl_settled(fill, outcome="yes")
    # Settlement term only (no fee)
    assert pnl == pytest.approx(-52)


def test_yes_bid_flip_no_wins():
    """Maker yes_bid at 48, NO wins.
    Maker P&L: long YES at 48 → -48 per contract.
    Taker flip: sold YES at 48 → owes 0 → +48 per contract."""
    fill = {"side": "yes_bid", "price": 48, "size": 1}
    pnl = flip_fill_to_taker_pnl_settled(fill, outcome="no")
    assert pnl == pytest.approx(48)


def test_yes_bid_flip_scales_with_size():
    fill = {"side": "yes_bid", "price": 48, "size": 5}
    pnl = flip_fill_to_taker_pnl_settled(fill, outcome="no")
    assert pnl == pytest.approx(48 * 5)


# ---------- flip no_bid maker → no-seller taker (settles) --------------

def test_no_bid_flip_no_wins():
    """Maker no_bid at 50, NO wins.
    Maker P&L: long NO at 50 → +50 per contract.
    Taker flip: sold NO at 50 → owes 100 at NO-settle → -50 per contract."""
    fill = {"side": "no_bid", "price": 50, "size": 1}
    pnl = flip_fill_to_taker_pnl_settled(fill, outcome="no")
    assert pnl == pytest.approx(-50)


def test_no_bid_flip_yes_wins():
    """Maker no_bid at 50, YES wins.
    Maker P&L: long NO at 50 → -50 per contract.
    Taker flip: sold NO at 50 → owes 0 → +50 per contract."""
    fill = {"side": "no_bid", "price": 50, "size": 1}
    pnl = flip_fill_to_taker_pnl_settled(fill, outcome="yes")
    assert pnl == pytest.approx(50)


# ---------- missing outcome → contribute 0 -----------------------------

def test_flip_unsettled_outcome_is_zero():
    """If outcome is None (not in cache), the fill contributes 0."""
    fill = {"side": "yes_bid", "price": 50, "size": 1}
    pnl = flip_fill_to_taker_pnl_settled(fill, outcome=None)
    assert pnl == 0


# ---------- aggregate per prefix ---------------------------------------

def test_aggregate_per_prefix_groups_correctly():
    """aggregate_per_prefix sums taker P&L per prefix7 with proper fee handling."""
    fills = [
        {"ticker": "tsc-mlb-az-tex-2026-05-11-7pt5",
         "side": "yes_bid", "price": 48, "size": 1},
        {"ticker": "tsc-mlb-nyy-bal-2026-05-11-8pt5",
         "side": "no_bid", "price": 50, "size": 2},
        {"ticker": "aec-nhl-col-min-2026-05-11",
         "side": "yes_bid", "price": 30, "size": 1},
    ]
    outcomes = {
        "tsc-mlb-az-tex-2026-05-11-7pt5": "no",
        "tsc-mlb-nyy-bal-2026-05-11-8pt5": "no",
        "aec-nhl-col-min-2026-05-11": "yes",
    }
    agg = aggregate_per_prefix(fills, outcomes)
    # tsc-mlb fills:
    #   fill1: yes_bid @48 size=1, outcome=no → taker settled +48
    #          taker fee at P=0.48: 0.02 * 0.48 * 0.52 * 100 ≈ 0.4992 × 1 = 0.4992
    #   fill2: no_bid @50 size=2, outcome=no → taker settled -50 × 2 = -100
    #          taker fee at P=0.50: 0.5 × 2 = 1.0
    #   net = 48 + (-100) - 0.4992 - 1.0 = -53.4992
    tsc_mlb = agg["tsc-mlb"]
    assert tsc_mlb["n_fills"] == 2
    assert tsc_mlb["settled_pnl_c"] == pytest.approx(-52)  # 48 + (-100)
    assert tsc_mlb["taker_fees_c"] == pytest.approx(1.4992, abs=0.01)
    assert tsc_mlb["net_pnl_c"] == pytest.approx(-53.4992, abs=0.01)
    # aec-nhl: yes_bid @30 size=1, outcome=yes → taker settled = -(100-30) = -70
    #          taker fee at P=0.30: 0.02 * 0.3 * 0.7 * 100 = 0.42 × 1
    aec_nhl = agg["aec-nhl"]
    assert aec_nhl["n_fills"] == 1
    assert aec_nhl["settled_pnl_c"] == pytest.approx(-70)
    assert aec_nhl["taker_fees_c"] == pytest.approx(0.42, abs=0.01)


def test_aggregate_skips_unsettled():
    """Fills without an outcome in the dict count toward n_fills_skipped, not P&L."""
    fills = [
        {"ticker": "tsc-mlb-az-tex-2026-05-11-7pt5",
         "side": "yes_bid", "price": 48, "size": 1},
    ]
    outcomes = {}  # no resolutions
    agg = aggregate_per_prefix(fills, outcomes)
    tsc_mlb = agg["tsc-mlb"]
    assert tsc_mlb["n_fills"] == 1
    assert tsc_mlb["n_fills_skipped"] == 1
    assert tsc_mlb["settled_pnl_c"] == 0
    # Fees still apply (we'd have paid them as a taker even if settlement unknown)
    # ... actually let's say no — we only count the trades we can evaluate
    assert tsc_mlb["taker_fees_c"] == 0
    assert tsc_mlb["net_pnl_c"] == 0


# ---------- per-prefix metadata ----------------------------------------

def test_aggregate_per_prefix_has_contract_count():
    fills = [
        {"ticker": "tsc-mlb-x", "side": "yes_bid", "price": 50, "size": 3},
        {"ticker": "tsc-mlb-y", "side": "no_bid", "price": 50, "size": 2},
    ]
    outcomes = {"tsc-mlb-x": "no", "tsc-mlb-y": "yes"}
    agg = aggregate_per_prefix(fills, outcomes)
    assert agg["tsc-mlb"]["n_contracts"] == 5
