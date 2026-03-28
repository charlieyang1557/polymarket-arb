# tests/test_calibration_category.py
"""Tests for calibration category + timing analysis."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.kalshi_calibration_category import (
    category_filtered_analysis,
    significant_summary,
    compute_timing_gaps,
    sample_anomalous_tickers,
    sports_focus_analysis,
)


def _m(ticker="T1", category="Sports", final_price=65, yes_won=True,
       volume=500, close_time="2026-03-27T20:00:00Z",
       last_trade_time="2026-03-27T19:45:00Z",
       settlement_ts="2026-03-27T23:00:00Z", title="Test Game"):
    """Helper to build a market dict."""
    return {
        "ticker": ticker, "category": category, "final_price": final_price,
        "yes_won": yes_won, "volume": volume, "close_time": close_time,
        "last_trade_time": last_trade_time, "settlement_ts": settlement_ts,
        "title": title,
    }


# --- category_filtered_analysis ---

def test_category_filtered_splits_by_category():
    markets = [
        _m(category="Sports", final_price=65, yes_won=True, volume=200),
        _m(category="Sports", final_price=62, yes_won=True, volume=200),
        _m(category="Financials", final_price=35, yes_won=False, volume=200),
    ]
    result = category_filtered_analysis(markets, min_volume=100)
    assert "Sports" in result
    assert "Financials" in result


def test_category_filtered_respects_volume_filter():
    """Markets below min_volume excluded."""
    markets = [
        _m(category="Sports", final_price=65, yes_won=True, volume=200),
        _m(category="Sports", final_price=62, yes_won=True, volume=50),  # filtered
    ]
    result = category_filtered_analysis(markets, min_volume=100)
    # Only 1 market survives in Sports 60-70c bucket
    sports = {b["bucket"]: b for b in result["Sports"]}
    assert sports["60-70c"]["count"] == 1


def test_category_filtered_empty_category_after_filter():
    """Category disappears if all markets below volume threshold."""
    markets = [
        _m(category="Crypto", final_price=45, yes_won=True, volume=10),
    ]
    result = category_filtered_analysis(markets, min_volume=100)
    assert "Crypto" not in result


# --- significant_summary ---

def test_significant_summary_filters_by_n_and_p():
    cat_results = {
        "Sports": [
            {"bucket": "60-70c", "count": 30, "p_value": 0.01,
             "win_rate": 75.0, "expected": 65.0, "edge": 10.0},
            {"bucket": "40-50c", "count": 5, "p_value": 0.01,
             "win_rate": 60.0, "expected": 45.0, "edge": 15.0},  # N too low
            {"bucket": "50-60c", "count": 30, "p_value": 0.20,
             "win_rate": 56.0, "expected": 55.0, "edge": 1.0},  # p too high
        ],
    }
    result = significant_summary(cat_results, min_n=20, max_p=0.05)
    assert len(result) == 1
    assert result[0]["category"] == "Sports"
    assert result[0]["bucket"] == "60-70c"


def test_significant_summary_empty_when_nothing_passes():
    cat_results = {
        "Crypto": [
            {"bucket": "70-80c", "count": 5, "p_value": 0.50,
             "win_rate": 76.0, "expected": 75.0, "edge": 1.0},
        ],
    }
    result = significant_summary(cat_results, min_n=20, max_p=0.05)
    assert result == []


# --- compute_timing_gaps ---

def test_timing_gaps_basic():
    """Close-to-settlement and last_trade-to-close computed correctly."""
    markets = [
        _m(close_time="2026-03-27T20:00:00Z",
           last_trade_time="2026-03-27T19:45:00Z",
           settlement_ts="2026-03-27T23:00:00Z"),
    ]
    result = compute_timing_gaps(markets)
    assert len(result) == 1
    # close_to_settlement = 3h
    assert abs(result[0]["close_to_settlement_hours"] - 3.0) < 0.01
    # last_trade_to_close = 0.25h
    assert abs(result[0]["last_trade_to_close_hours"] - 0.25) < 0.01


def test_timing_gaps_skips_missing_timestamps():
    """Markets without timestamps get None gaps."""
    markets = [
        _m(close_time="", last_trade_time=None, settlement_ts=""),
    ]
    result = compute_timing_gaps(markets)
    assert len(result) == 1
    assert result[0]["close_to_settlement_hours"] is None
    assert result[0]["last_trade_to_close_hours"] is None


def test_timing_gaps_within_30min_flag():
    """Flag for last_trade within 30min of close."""
    markets = [
        _m(close_time="2026-03-27T20:00:00Z",
           last_trade_time="2026-03-27T19:40:00Z",  # 20min before close
           settlement_ts="2026-03-27T23:00:00Z"),
        _m(close_time="2026-03-27T20:00:00Z",
           last_trade_time="2026-03-27T18:00:00Z",  # 2h before close
           settlement_ts="2026-03-27T23:00:00Z"),
    ]
    result = compute_timing_gaps(markets)
    assert result[0]["trade_within_30min_of_close"] is True
    assert result[1]["trade_within_30min_of_close"] is False


# --- sample_anomalous_tickers ---

def test_sample_anomalous_returns_max_10():
    markets = [_m(ticker=f"T{i}", category="Financials", final_price=35)
               for i in range(20)]
    result = sample_anomalous_tickers(markets, "Financials", 30, 40)
    assert len(result) <= 10


def test_sample_anomalous_filters_bucket():
    markets = [
        _m(ticker="T1", category="Financials", final_price=35),
        _m(ticker="T2", category="Financials", final_price=65),  # wrong bucket
        _m(ticker="T3", category="Sports", final_price=35),      # wrong category
    ]
    result = sample_anomalous_tickers(markets, "Financials", 30, 40)
    assert len(result) == 1
    assert result[0]["ticker"] == "T1"


# --- sports_focus_analysis ---

def test_sports_focus_55_75():
    """Sports 55-75c focus returns edge and p-value."""
    markets = []
    # 60 markets at 65c, 48 YES wins (80% vs 65% expected)
    for i in range(48):
        markets.append(_m(final_price=65, yes_won=True, volume=200,
                          category="Sports"))
    for i in range(12):
        markets.append(_m(final_price=65, yes_won=False, volume=200,
                          category="Sports"))

    result = sports_focus_analysis(markets, min_volume=100)
    assert result["n"] == 60
    assert result["win_rate"] == 80.0
    assert result["expected"] == 65.0
    assert result["edge"] == 15.0
    assert result["p_value"] < 0.05


def test_sports_focus_empty():
    """No sports markets in range → empty result."""
    markets = [_m(final_price=45, category="Sports", volume=200)]
    result = sports_focus_analysis(markets, min_volume=100)
    assert result["n"] == 0


def test_sports_focus_volume_filter():
    """Low-volume sports markets excluded from focus."""
    markets = [_m(final_price=65, yes_won=True, volume=50, category="Sports")]
    result = sports_focus_analysis(markets, min_volume=100)
    assert result["n"] == 0
