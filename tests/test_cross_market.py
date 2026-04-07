import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_cross_market import (
    bucket_for_lag,
    detect_market_lag,
    group_snapshots_by_event,
)


def _ts(seconds: int) -> str:
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def test_detect_market_lag_finds_45_second_match():
    market_a = [
        {"timestamp": _ts(0), "mid_price": 50.0},
        {"timestamp": _ts(30), "mid_price": 53.0},
    ]
    market_b = [
        {"timestamp": _ts(0), "mid_price": 40.0},
        {"timestamp": _ts(75), "mid_price": 42.0},
    ]

    assert detect_market_lag(market_a, market_b) == 45.0


def test_detect_market_lag_returns_never_when_b_never_moves():
    market_a = [
        {"timestamp": _ts(0), "mid_price": 50.0},
        {"timestamp": _ts(15), "mid_price": 53.0},
    ]
    market_b = [
        {"timestamp": _ts(0), "mid_price": 40.0},
        {"timestamp": _ts(300), "mid_price": 41.0},
    ]

    assert detect_market_lag(market_a, market_b) is None
    assert bucket_for_lag(None) == "never"


def test_group_snapshots_by_event_groups_rows():
    rows = [
        {"event_slug": "nba-lal-bos", "market_slug": "aec-nba-lal-bos-2026-04-01"},
        {"event_slug": "nba-lal-bos", "market_slug": "asc-nba-lal-bos-2026-04-01-pos-2pt5"},
        {"event_slug": "mlb-nyy-bos", "market_slug": "aec-mlb-nyy-bos-2026-04-01"},
    ]

    grouped = group_snapshots_by_event(rows)

    assert sorted(grouped.keys()) == ["mlb-nyy-bos", "nba-lal-bos"]
    assert [row["market_slug"] for row in grouped["nba-lal-bos"]] == [
        "aec-nba-lal-bos-2026-04-01",
        "asc-nba-lal-bos-2026-04-01-pos-2pt5",
    ]


def test_bucket_for_lag_handles_edges():
    assert bucket_for_lag(30.0) == "30-60s"
    assert bucket_for_lag(300.0) == "300s+"


def test_detect_market_lag_requires_same_direction():
    market_a = [
        {"timestamp": _ts(0), "mid_price": 50.0},
        {"timestamp": _ts(10), "mid_price": 53.0},
    ]
    market_b = [
        {"timestamp": _ts(0), "mid_price": 40.0},
        {"timestamp": _ts(40), "mid_price": 38.0},
    ]

    assert detect_market_lag(market_a, market_b) is None
