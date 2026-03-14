# tests/test_dynamic_spread.py
"""Tests for volatility-based dynamic spread."""
from datetime import datetime, timezone, timedelta
from src.mm.state import dynamic_spread


def test_min_spread_when_no_history():
    """Empty history → return min_spread."""
    assert dynamic_spread([], datetime.now(timezone.utc)) == 2


def test_min_spread_when_few_points():
    """< 3 data points → return min_spread."""
    now = datetime.now(timezone.utc)
    history = [(now - timedelta(seconds=30), 48.0),
               (now, 48.5)]
    assert dynamic_spread(history, now) == 2


def test_min_spread_stable_market():
    """Stable market (all same price) → stdev=0 → min_spread."""
    now = datetime.now(timezone.utc)
    history = [(now - timedelta(seconds=i * 10), 48.0) for i in range(6)]
    assert dynamic_spread(history, now) == 2


def test_spread_scales_with_volatility():
    """vol=3 → spread = round(3*2) = 6c."""
    now = datetime.now(timezone.utc)
    # stdev of [45, 48, 51] = 3.0
    history = [
        (now - timedelta(seconds=40), 45.0),
        (now - timedelta(seconds=20), 48.0),
        (now, 51.0),
    ]
    assert dynamic_spread(history, now) == 6


def test_spread_moderate_volatility():
    """vol ~1.5 → spread = round(1.5*2) = 3c."""
    now = datetime.now(timezone.utc)
    # stdev of [46, 48, 49, 48] ≈ 1.29 → round(2.58) = 3
    history = [
        (now - timedelta(seconds=40), 46.0),
        (now - timedelta(seconds=30), 48.0),
        (now - timedelta(seconds=20), 49.0),
        (now, 48.0),
    ]
    assert dynamic_spread(history, now) == 3


def test_spread_high_volatility():
    """Large swings → wide spread."""
    now = datetime.now(timezone.utc)
    # stdev of [40, 50, 42, 52] ≈ 5.89 → round(11.78) = 12
    history = [
        (now - timedelta(seconds=40), 40.0),
        (now - timedelta(seconds=30), 50.0),
        (now - timedelta(seconds=20), 42.0),
        (now, 52.0),
    ]
    assert dynamic_spread(history, now) == 12


def test_ignores_old_data():
    """Points outside lookback window are excluded."""
    now = datetime.now(timezone.utc)
    history = [
        # Old volatile data (outside 5min window)
        (now - timedelta(minutes=10), 30.0),
        (now - timedelta(minutes=8), 60.0),
        # Recent stable data (inside window)
        (now - timedelta(seconds=30), 48.0),
        (now - timedelta(seconds=20), 48.0),
        (now, 48.0),
    ]
    assert dynamic_spread(history, now) == 2


def test_custom_min_spread():
    """Respect custom min_spread parameter."""
    now = datetime.now(timezone.utc)
    history = [(now - timedelta(seconds=i * 10), 48.0) for i in range(5)]
    assert dynamic_spread(history, now, min_spread=3) == 3
