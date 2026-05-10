# tests/test_research_counterfactual.py
"""Tests for Phase 1.2 counterfactual settlement P&L analysis.

Pure functions tested with synthetic data; no DB or SDK access.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts.research.counterfactual_settlement_pnl import (
    parse_resolution,
    fill_settlement_pnl,
    classify_aligned,
    decompose_frequency_magnitude,
    apply_capture_haircut,
    derive_unhedged_subset,
    aggregate_by_key,
)


# ---------- parse_resolution ----------

def test_parse_resolution_yes_won():
    """long-true side has settled price '1' → YES won."""
    market = {
        "marketSides": [
            {"long": True, "price": "1", "description": "team A"},
            {"long": False, "price": "0", "description": "team B"},
        ],
    }
    assert parse_resolution(market) == "yes"


def test_parse_resolution_no_won():
    market = {
        "marketSides": [
            {"long": True, "price": "0", "description": "team A"},
            {"long": False, "price": "1", "description": "team B"},
        ],
    }
    assert parse_resolution(market) == "no"


def test_parse_resolution_unresolved():
    """Both sides at non-binary settlement values → not yet settled."""
    market = {
        "marketSides": [
            {"long": True, "price": "0.55"},
            {"long": False, "price": "0.45"},
        ],
    }
    assert parse_resolution(market) is None


def test_parse_resolution_uses_outcome_prices_fallback():
    """Falls back to outcomePrices if marketSides missing/incomplete."""
    market = {
        "marketSides": [],
        "outcomes": ["+2.5", "-2.5"],
        "outcomePrices": ["1", "0"],
    }
    # First outcome is the long/YES side by Polymarket convention
    assert parse_resolution(market) == "yes"


def test_parse_resolution_missing_data():
    assert parse_resolution({}) is None
    assert parse_resolution({"marketSides": None}) is None


# ---------- fill_settlement_pnl ----------

def test_fill_pnl_yes_bid_yes_settles():
    """Bought YES at 60c, size 2, YES wins. P&L = (100-60)*2 = +80c."""
    pnl = fill_settlement_pnl(side="yes_bid", price_cents=60, size=2, settled_yes=True)
    assert pnl == 80


def test_fill_pnl_yes_bid_no_settles():
    """Bought YES at 60c, size 2, NO wins. P&L = -60*2 = -120c."""
    pnl = fill_settlement_pnl(side="yes_bid", price_cents=60, size=2, settled_yes=False)
    assert pnl == -120


def test_fill_pnl_no_bid_no_settles():
    """Bought NO at 40c, size 3, NO wins. P&L = (100-40)*3 = +180c."""
    pnl = fill_settlement_pnl(side="no_bid", price_cents=40, size=3, settled_yes=False)
    assert pnl == 180


def test_fill_pnl_no_bid_yes_settles():
    """Bought NO at 40c, size 3, YES wins. P&L = -40*3 = -120c."""
    pnl = fill_settlement_pnl(side="no_bid", price_cents=40, size=3, settled_yes=True)
    assert pnl == -120


def test_fill_pnl_zero_size():
    assert fill_settlement_pnl(side="yes_bid", price_cents=50, size=0, settled_yes=True) == 0


def test_fill_pnl_unknown_side_raises():
    with pytest.raises(ValueError):
        fill_settlement_pnl(side="other", price_cents=50, size=1, settled_yes=True)


# ---------- classify_aligned ----------

def test_classify_aligned_yes_bid_wins_yes():
    assert classify_aligned("yes_bid", settled_yes=True) is True


def test_classify_aligned_no_bid_wins_no():
    assert classify_aligned("no_bid", settled_yes=False) is True


def test_classify_aligned_yes_bid_loses_no():
    assert classify_aligned("yes_bid", settled_yes=False) is False


def test_classify_aligned_no_bid_loses_yes():
    assert classify_aligned("no_bid", settled_yes=True) is False


# ---------- decompose_frequency_magnitude ----------

def test_decompose_basic():
    """Paper Equation 7: E[π] = (WR - 0.5)(W+L) + 0.5(W-L)."""
    # Construct: 7 wins of +20c, 3 losses of -30c, all size=1
    fills_with_pnl = (
        [{"pnl_cents": 20, "size": 1} for _ in range(7)]
        + [{"pnl_cents": -30, "size": 1} for _ in range(3)]
    )
    result = decompose_frequency_magnitude(fills_with_pnl)
    # 10 contracts, 7 wins, win rate 0.7
    # avg win = 20, avg loss magnitude = 30
    # frequency edge = (0.7 - 0.5)(20 + 30) = +10
    # magnitude edge = 0.5(20 - 30) = -5
    # total = 7*20 - 3*30 = 140 - 90 = 50; /10 contracts = 5
    assert result["n_contracts"] == 10
    assert result["wins"] == 7
    assert result["losses"] == 3
    assert result["win_rate"] == pytest.approx(0.7)
    assert result["avg_win_cents"] == pytest.approx(20.0)
    assert result["avg_loss_cents"] == pytest.approx(30.0)
    assert result["frequency_edge_cents"] == pytest.approx(10.0)
    assert result["magnitude_edge_cents"] == pytest.approx(-5.0)
    assert result["mean_pnl_cents"] == pytest.approx(5.0)


def test_decompose_handles_zero_losses():
    """All wins, no losses — magnitude_edge undefined; report safely."""
    fills_with_pnl = [{"pnl_cents": 50, "size": 1} for _ in range(5)]
    result = decompose_frequency_magnitude(fills_with_pnl)
    assert result["wins"] == 5
    assert result["losses"] == 0
    assert result["win_rate"] == pytest.approx(1.0)
    assert result["mean_pnl_cents"] == pytest.approx(50.0)


def test_decompose_handles_size_weighting():
    """Contract count weights, not fill count."""
    fills_with_pnl = [
        {"pnl_cents": 100, "size": 5},  # 5 winning contracts at +20 each? No — pnl_cents is total
        {"pnl_cents": -60, "size": 3},  # 3 losing contracts at -20 each
    ]
    # 8 contracts total: 5 wins, 3 losses
    # avg win/contract = 100/5 = 20; avg loss magnitude/contract = 60/3 = 20
    result = decompose_frequency_magnitude(fills_with_pnl)
    assert result["n_contracts"] == 8
    assert result["wins"] == 5
    assert result["losses"] == 3
    assert result["avg_win_cents"] == pytest.approx(20.0)
    assert result["avg_loss_cents"] == pytest.approx(20.0)


def test_decompose_empty():
    result = decompose_frequency_magnitude([])
    assert result["n_contracts"] == 0
    assert result["mean_pnl_cents"] == 0


# ---------- apply_capture_haircut ----------

def test_apply_capture_haircut_default():
    """Default 50% haircut."""
    assert apply_capture_haircut(100) == 50


def test_apply_capture_haircut_custom():
    assert apply_capture_haircut(100, multiplier=0.7) == 70


def test_apply_capture_haircut_negative_pnl():
    """Negative P&L is preserved fully — haircut applies to potential edge, not loss exposure."""
    # Conservative interpretation: haircut shrinks gains but losses remain at full size
    # (because in a real strategy losses still happen at full magnitude)
    assert apply_capture_haircut(-100) == -100


# ---------- derive_unhedged_subset ----------

def test_derive_unhedged_no_offsetting_fills():
    """All yes_bid fills, no offsets → all unhedged."""
    fills = [
        {"ticker": "m1", "side": "yes_bid", "size": 2, "price_cents": 50,
         "filled_at": "2026-01-01T10:00:00Z"},
        {"ticker": "m1", "side": "yes_bid", "size": 1, "price_cents": 51,
         "filled_at": "2026-01-01T10:01:00Z"},
    ]
    unhedged = derive_unhedged_subset(fills)
    # Net YES inventory = 3, all unhedged (no NO buys)
    assert len(unhedged) == 1  # one market entry
    assert unhedged[0]["ticker"] == "m1"
    assert unhedged[0]["net_yes_inventory"] == 3
    assert unhedged[0]["net_side"] == "yes"


def test_derive_unhedged_balanced_fills():
    """Equal yes_bid and no_bid → fully hedged, no unhedged inventory."""
    fills = [
        {"ticker": "m1", "side": "yes_bid", "size": 2, "price_cents": 50,
         "filled_at": "2026-01-01T10:00:00Z"},
        {"ticker": "m1", "side": "no_bid", "size": 2, "price_cents": 51,
         "filled_at": "2026-01-01T10:01:00Z"},
    ]
    unhedged = derive_unhedged_subset(fills)
    # Net inventory = 0 (yes - no = 0)
    assert len(unhedged) == 0


def test_derive_unhedged_partial_hedge():
    """3 yes_bid, 1 no_bid → 2 net YES unhedged."""
    fills = [
        {"ticker": "m1", "side": "yes_bid", "size": 3, "price_cents": 50,
         "filled_at": "2026-01-01T10:00:00Z"},
        {"ticker": "m1", "side": "no_bid", "size": 1, "price_cents": 51,
         "filled_at": "2026-01-01T10:01:00Z"},
    ]
    unhedged = derive_unhedged_subset(fills)
    assert len(unhedged) == 1
    assert unhedged[0]["net_yes_inventory"] == 2


def test_derive_unhedged_net_no():
    """1 yes_bid, 3 no_bid → -2 net (long NO)."""
    fills = [
        {"ticker": "m1", "side": "yes_bid", "size": 1, "price_cents": 50,
         "filled_at": "2026-01-01T10:00:00Z"},
        {"ticker": "m1", "side": "no_bid", "size": 3, "price_cents": 51,
         "filled_at": "2026-01-01T10:01:00Z"},
    ]
    unhedged = derive_unhedged_subset(fills)
    assert len(unhedged) == 1
    assert unhedged[0]["net_yes_inventory"] == -2
    assert unhedged[0]["net_side"] == "no"


# ---------- aggregate_by_key ----------

def test_aggregate_by_ticker():
    fills_with_pnl = [
        {"ticker": "m1", "pnl_cents": 50, "size": 2},
        {"ticker": "m1", "pnl_cents": -30, "size": 1},
        {"ticker": "m2", "pnl_cents": 100, "size": 4},
    ]
    out = aggregate_by_key(fills_with_pnl, key="ticker")
    assert "m1" in out and "m2" in out
    assert out["m1"]["total_pnl_cents"] == pytest.approx(20.0)
    assert out["m1"]["n_contracts"] == 3
    assert out["m2"]["total_pnl_cents"] == pytest.approx(100.0)


def test_aggregate_by_side():
    fills_with_pnl = [
        {"side": "yes_bid", "pnl_cents": 50, "size": 2},
        {"side": "no_bid", "pnl_cents": -30, "size": 1},
        {"side": "yes_bid", "pnl_cents": 100, "size": 4},
    ]
    out = aggregate_by_key(fills_with_pnl, key="side")
    assert out["yes_bid"]["total_pnl_cents"] == pytest.approx(150.0)
    assert out["yes_bid"]["n_contracts"] == 6
    assert out["no_bid"]["total_pnl_cents"] == pytest.approx(-30.0)
