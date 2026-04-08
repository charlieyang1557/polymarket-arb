import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_cross_market import (
    bucket_for_lag,
    classify_follower_outcome,
    detect_market_lag,
    group_snapshots_by_event,
    parse_timestamp,
    simulate_trade,
    taker_fee_cents,
)


def _ts(seconds: int) -> str:
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _snapshot(seconds: int, mid: float, best_bid: float | None = None,
              best_ask: float | None = None) -> dict:
    return {
        "timestamp": _ts(seconds),
        "mid_price": mid,
        "best_bid": mid if best_bid is None else best_bid,
        "best_ask": mid if best_ask is None else best_ask,
    }


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


def test_classify_follower_correct():
    trigger = {
        "timestamp": parse_timestamp(_ts(0)),
        "direction": 1,
        "change": 3.0,
    }
    follower = [
        _snapshot(0, 40.0),
        _snapshot(45, 42.0),
    ]

    outcome = classify_follower_outcome(trigger, follower)

    assert outcome == {
        "outcome": "correct",
        "follower_change": 2.0,
        "lag_seconds": 45.0,
    }


def test_classify_follower_wrong():
    trigger = {
        "timestamp": parse_timestamp(_ts(0)),
        "direction": 1,
        "change": 3.0,
    }
    follower = [
        _snapshot(0, 40.0),
        _snapshot(30, 38.0),
    ]

    outcome = classify_follower_outcome(trigger, follower)

    assert outcome == {
        "outcome": "wrong",
        "follower_change": -2.0,
        "lag_seconds": 30.0,
    }


def test_classify_follower_flat():
    trigger = {
        "timestamp": parse_timestamp(_ts(0)),
        "direction": 1,
        "change": 3.0,
    }
    follower = [
        _snapshot(0, 40.0),
        _snapshot(120, 41.0),
        _snapshot(300, 41.0),
    ]

    outcome = classify_follower_outcome(trigger, follower)

    assert outcome == {
        "outcome": "flat",
        "follower_change": None,
        "lag_seconds": None,
    }


def test_classify_follower_respects_5min_window():
    trigger = {
        "timestamp": parse_timestamp(_ts(0)),
        "direction": 1,
        "change": 3.0,
    }
    follower = [
        _snapshot(0, 40.0),
        _snapshot(400, 43.0),
    ]

    outcome = classify_follower_outcome(trigger, follower)

    assert outcome == {
        "outcome": "flat",
        "follower_change": None,
        "lag_seconds": None,
    }


def test_taker_fee_at_midpoint():
    assert taker_fee_cents(50) == 0.5


def test_taker_fee_at_extremes():
    assert taker_fee_cents(90) == 0.18


def test_simulate_trade_take_profit():
    trigger = {
        "timestamp": parse_timestamp(_ts(0)),
        "direction": 1,
        "change": 3.0,
    }
    follower = [
        _snapshot(0, 49.0, best_bid=49.0, best_ask=50.0),
        _snapshot(10, 51.0, best_bid=50.0, best_ask=52.0),
        _snapshot(40, 53.0, best_bid=52.0, best_ask=54.0),
    ]

    trade = simulate_trade(trigger, follower)

    assert trade["entered"] is True
    assert trade["exit_reason"] == "take_profit"
    assert trade["entry_price"] == 52.0
    assert trade["exit_price"] == 53.0
    assert trade["pnl_cents"] == 0.0026


def test_simulate_trade_timeout():
    trigger = {
        "timestamp": parse_timestamp(_ts(0)),
        "direction": 1,
        "change": 3.0,
    }
    follower = [
        _snapshot(0, 49.0, best_bid=49.0, best_ask=50.0),
        _snapshot(10, 50.0, best_bid=49.0, best_ask=51.0),
        _snapshot(300, 50.0, best_bid=49.0, best_ask=51.0),
    ]

    trade = simulate_trade(trigger, follower)

    assert trade["entered"] is True
    assert trade["exit_reason"] == "timeout"
    assert trade["entry_price"] == 51.0
    assert trade["exit_price"] == 50.0
    assert trade["pnl_cents"] == -1.9998
