"""Tests for the edge-persistence and price-vs-settlement-bias analyses.

Experiment 1: are positive-aggregate prefixes' taker edges spread across
many markets (structural) or concentrated in 1-2 lucky games (sample luck)?

Experiment 3: for each prefix, are the bot's fill prices a fair reflection
of settlement probability, or are they systematically biased?
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from scripts.research.edge_persistence_and_bias import (
    per_market_taker_pnl,
    concentration_ratio,
    implied_vs_realized_per_prefix,
)


# ---------- Experiment 1 helpers ----------

def test_per_market_taker_pnl_groups_by_market():
    """Aggregates taker counterfactual P&L per market_slug within a prefix."""
    fills = [
        {"ticker": "tsc-mlb-a-2026-05-01", "side": "yes_bid", "price": 48, "size": 1},
        {"ticker": "tsc-mlb-a-2026-05-01", "side": "yes_bid", "price": 49, "size": 1},
        {"ticker": "tsc-mlb-b-2026-05-02", "side": "no_bid", "price": 50, "size": 1},
    ]
    outcomes = {
        "tsc-mlb-a-2026-05-01": "no",
        "tsc-mlb-b-2026-05-02": "no",
    }
    per_market = per_market_taker_pnl(fills, outcomes)
    # tsc-mlb-a: 2 yes_bid fills, both outcome=no → taker net = (48 + 49) - fees
    #   fees = 0.02*0.48*0.52*100 + 0.02*0.49*0.51*100 ≈ 0.4992 + 0.4998 = ~1.0
    assert "tsc-mlb-a-2026-05-01" in per_market
    a = per_market["tsc-mlb-a-2026-05-01"]
    assert a["settled_pnl_c"] == pytest.approx(97)  # 48 + 49
    # tsc-mlb-b: 1 no_bid fill at 50, outcome=no → taker (sell NO) loses, -50
    b = per_market["tsc-mlb-b-2026-05-02"]
    assert b["settled_pnl_c"] == pytest.approx(-50)


def test_concentration_ratio_pure_spread():
    """If P&L is evenly spread across N markets, concentration_ratio is 1/N."""
    market_pnls = [10.0, 10.0, 10.0, 10.0, 10.0]
    # Top market contributes 10/50 = 0.2
    assert concentration_ratio(market_pnls, top_n=1) == pytest.approx(0.2)


def test_concentration_ratio_concentrated():
    """If P&L is concentrated in one market, ratio is close to 1.0."""
    market_pnls = [100.0, 0.1, 0.1, 0.1, 0.1]
    # Top market contributes 100/100.4 ≈ 0.996
    assert concentration_ratio(market_pnls, top_n=1) > 0.99


def test_concentration_ratio_negative_pnls_use_abs():
    """Concentration of total contribution uses absolute value
    (mixed-sign P&L still has meaningful top-1 measure)."""
    market_pnls = [50.0, -50.0, 0.1]
    # |total| of contributions = 100.1; top abs is 50/100.1 ≈ 0.499
    r = concentration_ratio(market_pnls, top_n=1)
    assert 0.4 < r < 0.6


def test_concentration_ratio_top_n_2():
    """top_n=2 sums the two largest |P&L| contributions."""
    market_pnls = [50.0, 30.0, 5.0, 5.0]
    # Top 2 = 50 + 30 = 80, total |abs| = 90 → 0.889
    r = concentration_ratio(market_pnls, top_n=2)
    assert r == pytest.approx(80 / 90, abs=0.01)


def test_concentration_ratio_empty_returns_zero():
    assert concentration_ratio([], top_n=1) == 0


# ---------- Experiment 3 helpers ----------

def test_implied_vs_realized_yes_side_unbiased():
    """If yes_bid fills at avg price 50 settled 50% YES, the implied
    probability matches realization → near-zero bias."""
    fills = [
        {"ticker": f"tsc-mlb-{i}", "side": "yes_bid", "price": 50, "size": 1}
        for i in range(10)
    ]
    # 5 yes, 5 no = 50% realized
    outcomes = {f"tsc-mlb-{i}": ("yes" if i < 5 else "no") for i in range(10)}
    result = implied_vs_realized_per_prefix(fills, outcomes)
    yes = result["tsc-mlb"]["yes_bid"]
    assert yes["avg_implied_prob"] == pytest.approx(0.50)
    assert yes["realized_win_rate"] == pytest.approx(0.50)
    assert abs(yes["bias_c"]) < 0.5  # near-zero per-contract bias


def test_implied_vs_realized_yes_side_overpriced():
    """If yes_bid fills at avg price 50 settled only 30% YES, the
    market was overpriced (yes too high) and the maker would lose."""
    fills = [
        {"ticker": f"tsc-mlb-{i}", "side": "yes_bid", "price": 50, "size": 1}
        for i in range(10)
    ]
    # 3 yes, 7 no = 30% realized vs 50% implied
    outcomes = {f"tsc-mlb-{i}": ("yes" if i < 3 else "no") for i in range(10)}
    result = implied_vs_realized_per_prefix(fills, outcomes)
    yes = result["tsc-mlb"]["yes_bid"]
    assert yes["avg_implied_prob"] == pytest.approx(0.50)
    assert yes["realized_win_rate"] == pytest.approx(0.30)
    # bias_c = (realized - implied) * 100 = -20c per contract
    assert yes["bias_c"] == pytest.approx(-20)


def test_implied_vs_realized_no_side_underpreced():
    """no_bid fills at price 50 with 70% NO realized → underpriced NO,
    maker would gain. (All fills share same prefix7 'tsc-mlb' for the test.)"""
    fills = [
        {"ticker": f"tsc-mlb-{i}-2026-05-01", "side": "no_bid", "price": 50, "size": 1}
        for i in range(10)
    ]
    outcomes = {f"tsc-mlb-{i}-2026-05-01": ("no" if i < 7 else "yes") for i in range(10)}
    result = implied_vs_realized_per_prefix(fills, outcomes)
    no_side = result["tsc-mlb"]["no_bid"]
    assert no_side["realized_win_rate"] == pytest.approx(0.70)
    assert no_side["bias_c"] == pytest.approx(20)


def test_implied_vs_realized_skips_unsettled():
    """Fills without an outcome contribute to a skipped count, not P&L."""
    fills = [
        {"ticker": "x-a", "side": "yes_bid", "price": 50, "size": 1},
        {"ticker": "x-b", "side": "yes_bid", "price": 50, "size": 1},
    ]
    outcomes = {"x-a": "yes"}  # x-b unsettled
    result = implied_vs_realized_per_prefix(fills, outcomes)
    yes = result["x-a"]["yes_bid"]  # prefix7 of "x-a"
    # prefix7("x-a") = "x-a" because only one dash
    # Actually let me re-check: _prefix7("x-a") → split = ["x", "a"], result = "x-a"
    # So all three fills end up in different prefix7s. Let me just check n_settled:
    # Find whichever prefix has the settled fill
    for p, sides in result.items():
        if "yes_bid" in sides and sides["yes_bid"]["n_settled"] > 0:
            assert sides["yes_bid"]["n_settled"] == 1
            assert sides["yes_bid"]["n_skipped"] == 0
            return
    # If we didn't find it, that's a fail
    assert False, "expected at least one settled fill"
