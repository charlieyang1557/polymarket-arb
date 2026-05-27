"""Tests for the forward taker observer.

Hypothetical-taker rule: for each trade in the tape, we'd have taken
the SAME direction as the historical taker.

  maker.side=BUY (yes_bid hit), taker.side=SELL → hypothetical us SOLD
    YES at trade price. At settle: yes wins → (price - 100); no → price.

  maker.side=SELL (yes_ask hit), taker.side=BUY → hypothetical us BOUGHT
    YES at trade price. At settle: yes wins → (100 - price); no → -price.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from scripts.research.forward_taker_observer import (
    hypothetical_taker_pnl_settled,
    aggregate_forward_taker_per_prefix,
)


def test_taker_sold_yes_no_wins():
    """Maker BUY hit by taker SELL: we'd have sold YES at trade price."""
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_BUY",
        "taker_side": "ORDER_SIDE_SELL",
        "price_cents": 48,
        "quantity": 1,
    }
    pnl = hypothetical_taker_pnl_settled(trade, outcome="no")
    # short YES at 48, NO wins → +48
    assert pnl == pytest.approx(48)


def test_taker_sold_yes_yes_wins():
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_BUY",
        "taker_side": "ORDER_SIDE_SELL",
        "price_cents": 48,
        "quantity": 1,
    }
    pnl = hypothetical_taker_pnl_settled(trade, outcome="yes")
    # short YES at 48, YES wins → owes 100, received 48 → -52
    assert pnl == pytest.approx(-52)


def test_taker_bought_yes_yes_wins():
    """Maker SELL hit by taker BUY: we'd have bought YES at trade price."""
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_SELL",
        "taker_side": "ORDER_SIDE_BUY",
        "price_cents": 52,
        "quantity": 1,
    }
    pnl = hypothetical_taker_pnl_settled(trade, outcome="yes")
    # long YES at 52, YES wins → 100 - 52 = +48
    assert pnl == pytest.approx(48)


def test_taker_bought_yes_no_wins():
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_SELL",
        "taker_side": "ORDER_SIDE_BUY",
        "price_cents": 52,
        "quantity": 1,
    }
    pnl = hypothetical_taker_pnl_settled(trade, outcome="no")
    # long YES at 52, NO wins → -52
    assert pnl == pytest.approx(-52)


def test_pnl_scales_with_quantity():
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_BUY",
        "taker_side": "ORDER_SIDE_SELL",
        "price_cents": 50,
        "quantity": 10,
    }
    pnl = hypothetical_taker_pnl_settled(trade, outcome="no")
    assert pnl == pytest.approx(500)


def test_unsettled_returns_zero():
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_BUY",
        "taker_side": "ORDER_SIDE_SELL",
        "price_cents": 50,
        "quantity": 1,
    }
    assert hypothetical_taker_pnl_settled(trade, outcome=None) == 0


def test_ambiguous_sides_returns_zero():
    """If maker and taker side are the same (shouldn't happen in real
    trades), don't crash."""
    trade = {
        "market_slug": "tsc-mlb-x",
        "maker_side": "ORDER_SIDE_BUY",
        "taker_side": "ORDER_SIDE_BUY",  # invalid combo
        "price_cents": 50,
        "quantity": 1,
    }
    assert hypothetical_taker_pnl_settled(trade, outcome="yes") == 0


# ---------- aggregate per prefix ----------

def test_aggregate_groups_by_prefix7():
    trades = [
        {"market_slug": "tsc-mlb-a-2026", "maker_side": "ORDER_SIDE_BUY",
         "taker_side": "ORDER_SIDE_SELL", "price_cents": 48, "quantity": 1},
        {"market_slug": "tsc-mlb-b-2026", "maker_side": "ORDER_SIDE_SELL",
         "taker_side": "ORDER_SIDE_BUY", "price_cents": 52, "quantity": 1},
        {"market_slug": "aec-nhl-x-2026", "maker_side": "ORDER_SIDE_BUY",
         "taker_side": "ORDER_SIDE_SELL", "price_cents": 30, "quantity": 1},
    ]
    outcomes = {
        "tsc-mlb-a-2026": "no",   # sell YES at 48 + NO wins → +48
        "tsc-mlb-b-2026": "yes",  # buy YES at 52 + YES wins → +48
        "aec-nhl-x-2026": "yes",  # sell YES at 30 + YES wins → -70
    }
    agg = aggregate_forward_taker_per_prefix(trades, outcomes)
    assert agg["tsc-mlb"]["settled_pnl_c"] == pytest.approx(96)
    assert agg["tsc-mlb"]["n_settled_trades"] == 2
    assert agg["aec-nhl"]["settled_pnl_c"] == pytest.approx(-70)


def test_aggregate_counts_unsettled():
    trades = [
        {"market_slug": "tsc-mlb-x", "maker_side": "ORDER_SIDE_BUY",
         "taker_side": "ORDER_SIDE_SELL", "price_cents": 48, "quantity": 1},
    ]
    outcomes = {}  # no settlements
    agg = aggregate_forward_taker_per_prefix(trades, outcomes)
    assert agg["tsc-mlb"]["n_trades"] == 1
    assert agg["tsc-mlb"]["n_unsettled"] == 1
    assert agg["tsc-mlb"]["settled_pnl_c"] == 0


def test_aggregate_includes_taker_fees():
    """Aggregate should compute taker fees applied to total quantity."""
    trades = [
        {"market_slug": "tsc-mlb-x", "maker_side": "ORDER_SIDE_BUY",
         "taker_side": "ORDER_SIDE_SELL", "price_cents": 50, "quantity": 4},
    ]
    outcomes = {"tsc-mlb-x": "no"}  # +50 * 4 = +200, fee = 0.5 * 4 = 2.0
    agg = aggregate_forward_taker_per_prefix(trades, outcomes)
    assert agg["tsc-mlb"]["settled_pnl_c"] == pytest.approx(200)
    assert agg["tsc-mlb"]["taker_fees_c"] == pytest.approx(2.0)
    assert agg["tsc-mlb"]["net_pnl_c"] == pytest.approx(198)
