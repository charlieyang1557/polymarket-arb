"""Tests for daily scanner scoring and ranking."""
import sys
import os
import math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from scripts.kalshi_daily_scan import (
    deep_check, net_spread_cents, rank_candidates,
)


# -- Helpers ------------------------------------------------------------------

def _mock_client(trades_per_hour):
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "200"], ["0.46", "300"]],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    now = datetime.now(timezone.utc)
    num_trades = int(trades_per_hour)
    trades = []
    for i in range(num_trades):
        ts = (now - timedelta(seconds=i * (3600 / max(num_trades, 1)))).strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z")
        trades.append({
            "trade_id": f"t{i}",
            "created_time": ts,
            "count_fp": "2",
            "yes_price_dollars": "0.46",
        })
    client.get_trades.return_value = {"trades": trades}
    return client


# -- net_spread_cents ---------------------------------------------------------

def test_net_spread_positive():
    """Spread of 5c at midpoint 50c: maker_fee = 0.0175*50*50/100 = 0.4375c
    per side, round up to 1c each. net_spread = 5 - 2*1 = 3."""
    assert net_spread_cents(5, 50.0) == 3


def test_net_spread_zero_at_thin_spread():
    """Spread of 2c at midpoint 50c: fees eat the entire spread."""
    result = net_spread_cents(2, 50.0)
    assert result <= 0


def test_net_spread_high_midpoint():
    """At midpoint 90c: fee = ceil(0.0175*90*10/100) = ceil(0.1575) = 1c.
    Spread 4 → net = 4 - 2*1 = 2."""
    assert net_spread_cents(4, 90.0) == 2


def test_net_spread_low_midpoint():
    """At midpoint 10c: fee = ceil(0.0175*10*90/100) = ceil(0.1575) = 1c.
    Spread 4 → net = 4 - 2*1 = 2."""
    assert net_spread_cents(4, 10.0) == 2


def test_net_spread_midpoint_50_spread_3():
    """Midpoint 50c: fee per side = ceil(0.0175*50*50/100) = ceil(0.4375) = 1c.
    Spread 3 → net = 3 - 2 = 1."""
    assert net_spread_cents(3, 50.0) == 1


# -- deep_check ---------------------------------------------------------------

def test_deep_check_adds_trades_per_hour():
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "trades_per_hour" in result[0]
    assert result[0]["trades_per_hour"] > 0


def test_deep_check_adds_net_spread():
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "net_spread" in result[0]
    assert result[0]["net_spread"] > 0


def test_deep_check_adds_binding_queue():
    """binding_queue = max(yes_depth, no_depth)."""
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "binding_queue" in result[0]
    # yes_depth = 200+300=500, no_depth = 200+300=500 → binding = 500
    assert result[0]["binding_queue"] == 500


def test_deep_check_passes_good_market():
    client = _mock_client(100)
    candidates = [{"ticker": "GOOD", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is True


def test_deep_check_fails_huge_l1_queue():
    """Market with L1 best depth >= 20000 should fail."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "25000"], ["0.46", "300"]],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 36)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.46"}
              for i in range(100)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "BIGQ", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False
    assert result[0]["yes_best_depth"] == 25000
    # Also verify max_best_depth is exposed for display
    assert result[0]["max_best_depth"] == 25000


def test_deep_check_exposes_max_best_depth():
    """max_best_depth should be stored on candidate for display."""
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "max_best_depth" in result[0]
    # L1 is 200 on both sides → max = 200
    assert result[0]["max_best_depth"] == 200


def test_deep_check_fails_wide_net_spread():
    """Net spread > 8 should fail — wide spreads have asymmetric liquidity."""
    client = _mock_client(100)
    # spread=14 at midpoint 50 → net = 14 - 2*1 = 12 > 8
    candidates = [{"ticker": "WIDE", "spread": 14, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["net_spread"] == 12
    assert result[0].get("passes") is False


def test_deep_check_passes_net_spread_at_boundary():
    """Net spread == 8 should still pass (upper bound inclusive)."""
    client = _mock_client(100)
    # spread=10 at midpoint 50 → net = 10 - 2*1 = 8
    candidates = [{"ticker": "BOUNDARY", "spread": 10, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["net_spread"] == 8
    assert result[0].get("passes") is True


def test_deep_check_fails_empty_yes_book():
    """Market with no YES levels should fail."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 36)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.46"}
              for i in range(100)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "NOYES", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["yes_best_depth"] == 0
    assert result[0].get("passes") is False


def test_deep_check_fails_empty_no_book():
    """Market with no NO levels should fail."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "200"], ["0.46", "300"]],
            "no_dollars": [],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 36)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.46"}
              for i in range(100)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "NONO", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["no_best_depth"] == 0
    assert result[0].get("passes") is False


def test_deep_check_fails_low_freq():
    client = _mock_client(5)
    candidates = [{"ticker": "SLOW", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_fails_negative_net_spread():
    """Spread of 1c should fail — fees exceed spread."""
    client = _mock_client(100)
    candidates = [{"ticker": "THIN", "spread": 1, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_fails_expiring_soon():
    """Market expiring in 30 minutes should fail."""
    client = _mock_client(100)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    candidates = [{"ticker": "EXPIRING", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": soon}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


# -- rank_candidates ----------------------------------------------------------

def test_rank_candidates_ordering():
    """Best market = highest net_spread + lowest queue + highest freq."""
    candidates = [
        {"ticker": "A", "net_spread": 5, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
        {"ticker": "B", "net_spread": 3, "binding_queue": 500,
         "trades_per_hour": 20, "passes": True},
        {"ticker": "C", "net_spread": 1, "binding_queue": 1000,
         "trades_per_hour": 10, "passes": True},
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0]["ticker"] == "A"
    assert ranked[-1]["ticker"] == "C"


def test_rank_candidates_uses_average_ties():
    """Tied markets should get average rank."""
    candidates = [
        {"ticker": "A", "net_spread": 3, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
        {"ticker": "B", "net_spread": 3, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
    ]
    ranked = rank_candidates(candidates)
    # Both should have identical composite scores
    assert ranked[0]["composite_rank"] == ranked[1]["composite_rank"]


def test_rank_candidates_only_ranks_passing():
    """Non-passing candidates should not get composite_rank."""
    candidates = [
        {"ticker": "GOOD", "net_spread": 5, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
        {"ticker": "BAD", "net_spread": -1, "binding_queue": 100,
         "trades_per_hour": 50, "passes": False},
    ]
    ranked = rank_candidates(candidates)
    passing = [c for c in ranked if c.get("passes")]
    failing = [c for c in ranked if not c.get("passes")]
    assert len(passing) == 1
    assert passing[0]["ticker"] == "GOOD"
    assert "composite_rank" in passing[0]
    # Failing markets should not have composite_rank
    assert "composite_rank" not in failing[0]


def test_rank_candidates_mixed_strengths():
    """Market good at spread but bad at queue should rank middle."""
    candidates = [
        {"ticker": "SPREAD_KING", "net_spread": 10, "binding_queue": 5000,
         "trades_per_hour": 10, "passes": True},
        {"ticker": "ALL_ROUNDER", "net_spread": 5, "binding_queue": 200,
         "trades_per_hour": 30, "passes": True},
        {"ticker": "FREQ_KING", "net_spread": 2, "binding_queue": 100,
         "trades_per_hour": 100, "passes": True},
    ]
    ranked = rank_candidates(candidates)
    # FREQ_KING dominates 2/3 axes (queue=1, freq=1) → composite 1.67
    # ALL_ROUNDER is middle on all (2,2,2) → composite 2.0
    # SPREAD_KING dominates 1 axis but worst on 2 → composite 2.33
    assert ranked[0]["ticker"] == "FREQ_KING"
    assert ranked[1]["ticker"] == "ALL_ROUNDER"
    assert ranked[2]["ticker"] == "SPREAD_KING"
