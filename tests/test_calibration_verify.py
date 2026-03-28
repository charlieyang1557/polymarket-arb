# tests/test_calibration_verify.py
"""Tests for calibration data quality verification."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from scripts.kalshi_calibration_verify import (
    extract_markets_with_timestamps, _price_source_dist, _volume_by_bucket,
)


def _event(category="Sports", markets=None):
    return {"category": category, "title": "Evt", "markets": markets or []}


def _market(ticker="T1", result="yes", last_price="0.65", volume="1000",
            close_time="2026-03-27T00:00:00Z"):
    return {
        "ticker": ticker, "title": "Test", "result": result,
        "last_price_dollars": last_price,
        "volume_fp": volume,
        "close_time": close_time,
    }


def test_extract_preserves_price_source():
    """Price source field tracks which API field was used."""
    events = [_event(markets=[_market(last_price="0.65")])]
    result = extract_markets_with_timestamps(events)
    assert result[0]["price_source"] == "last_price_dollars"


def test_extract_preserves_close_time():
    events = [_event(markets=[_market(close_time="2026-03-27T12:00:00Z")])]
    result = extract_markets_with_timestamps(events)
    assert result[0]["close_time"] == "2026-03-27T12:00:00Z"


def test_extract_preserves_volume():
    events = [_event(markets=[_market(volume="5000")])]
    result = extract_markets_with_timestamps(events)
    assert result[0]["volume"] == 5000


def test_price_source_dist():
    markets = [
        {"price_source": "last_price_dollars"},
        {"price_source": "last_price_dollars"},
        {"price_source": "previous_yes_bid_dollars"},
    ]
    dist = _price_source_dist(markets)
    assert dist["last_price_dollars"] == 2
    assert dist["previous_yes_bid_dollars"] == 1


def test_volume_by_bucket():
    markets = [
        {"final_price": 65, "volume": 1000},
        {"final_price": 62, "volume": 500},
        {"final_price": 25, "volume": 200},
    ]
    result = _volume_by_bucket(markets)
    assert "60-70c" in result
    assert result["60-70c"]["count"] == 2
    assert result["60-70c"]["avg"] == 750
    assert "20-30c" in result
