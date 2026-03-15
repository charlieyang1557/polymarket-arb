"""Tests for daily scanner trade volume ranking."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from scripts.kalshi_daily_scan import deep_check


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


def test_deep_check_adds_trades_per_hour():
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48, "volume_24h": 1000}]
    result = deep_check(client, candidates, max_check=1)
    assert "trades_per_hour" in result[0]
    assert result[0]["trades_per_hour"] > 0


def test_deep_check_passes_with_high_freq():
    client = _mock_client(600)
    candidates = [{"ticker": "FAST", "spread": 5, "midpoint": 48, "volume_24h": 5000}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is True


def test_deep_check_fails_with_low_freq():
    client = _mock_client(50)
    candidates = [{"ticker": "SLOW", "spread": 5, "midpoint": 48, "volume_24h": 5000}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False
