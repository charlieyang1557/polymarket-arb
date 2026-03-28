# tests/test_full_scan.py
"""Tests for full market scanner diagnostic."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from scripts.kalshi_full_scan import (
    net_spread_cents, extract_candidates, build_category_summary,
)


def test_net_spread_mid50():
    # fee = ceil(0.0175 * 50 * 50 / 100) = ceil(0.4375) = 1
    assert net_spread_cents(5, 50.0) == 3


def test_extract_candidates_filters_expired():
    """Expired markets are excluded."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    events = [{
        "category": "Sports",
        "title": "Test Event",
        "markets": [
            {"ticker": "EXPIRED", "yes_bid_dollars": "0.45",
             "yes_ask_dollars": "0.55",
             "expected_expiration_time": past},
            {"ticker": "ACTIVE", "yes_bid_dollars": "0.45",
             "yes_ask_dollars": "0.55",
             "expected_expiration_time": future},
        ]
    }]
    result = extract_candidates(events)
    tickers = [c["ticker"] for c in result]
    assert "EXPIRED" not in tickers
    assert "ACTIVE" in tickers


def test_extract_candidates_filters_extreme_midpoint():
    """Midpoint outside 20-80 is excluded."""
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    events = [{
        "category": "Politics",
        "title": "Test",
        "markets": [
            {"ticker": "EXTREME_LOW", "yes_bid_dollars": "0.05",
             "yes_ask_dollars": "0.10",
             "expected_expiration_time": future},
            {"ticker": "EXTREME_HIGH", "yes_bid_dollars": "0.90",
             "yes_ask_dollars": "0.95",
             "expected_expiration_time": future},
            {"ticker": "GOOD_MID", "yes_bid_dollars": "0.45",
             "yes_ask_dollars": "0.55",
             "expected_expiration_time": future},
        ]
    }]
    result = extract_candidates(events)
    tickers = [c["ticker"] for c in result]
    assert "EXTREME_LOW" not in tickers
    assert "EXTREME_HIGH" not in tickers
    assert "GOOD_MID" in tickers


def test_build_category_summary():
    """Category summary groups and computes aggregates."""
    candidates = [
        {"category": "Sports", "spread": 3, "net_spread": 1,
         "trades_per_hour": 50},
        {"category": "Sports", "spread": 5, "net_spread": 3,
         "trades_per_hour": 30},
        {"category": "Politics", "spread": 10, "net_spread": 8,
         "trades_per_hour": 5},
    ]
    summary = build_category_summary(candidates)
    cats = {s["category"]: s for s in summary}

    assert cats["Sports"]["markets"] == 2
    assert cats["Sports"]["avg_spread"] == 4.0
    assert cats["Sports"]["pct_viable"] == 100.0  # both have net>=1

    assert cats["Politics"]["markets"] == 1
    assert cats["Politics"]["max_net_spread"] == 8
