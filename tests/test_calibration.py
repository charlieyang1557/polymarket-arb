# tests/test_calibration.py
"""Tests for Kalshi calibration study."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.kalshi_calibration import (
    extract_resolved_markets, bucket_analysis, _binomial_pvalue,
)


def _market(ticker="T1", result="yes", last_price="0.65", volume="1000",
            category="Sports", title="Test"):
    return {
        "ticker": ticker, "title": title, "result": result,
        "last_price_dollars": last_price,
        "volume_fp": volume,
    }


def _event(category="Sports", markets=None):
    return {"category": category, "title": "Evt", "markets": markets or []}


def test_extract_resolved_yes_win():
    events = [_event(markets=[_market(result="yes", last_price="0.65")])]
    result = extract_resolved_markets(events)
    assert len(result) == 1
    assert result[0]["yes_won"] is True
    assert result[0]["final_price"] == 65


def test_extract_resolved_no_win():
    events = [_event(markets=[_market(result="no", last_price="0.30")])]
    result = extract_resolved_markets(events)
    assert len(result) == 1
    assert result[0]["yes_won"] is False
    assert result[0]["final_price"] == 30


def test_extract_skips_non_binary():
    """Scalar or empty results are skipped."""
    events = [_event(markets=[
        _market(result="scalar", last_price="0.50"),
        _market(result="", last_price="0.50"),
    ])]
    result = extract_resolved_markets(events)
    assert len(result) == 0


def test_extract_skips_extreme_prices():
    """Prices at 0 or 100 are skipped."""
    events = [_event(markets=[
        _market(last_price="0.00"),
        _market(last_price="1.00"),
    ])]
    result = extract_resolved_markets(events)
    assert len(result) == 0


def test_extract_category_preserved():
    events = [_event(category="Politics",
                     markets=[_market(last_price="0.50")])]
    result = extract_resolved_markets(events)
    assert result[0]["category"] == "Politics"


def test_bucket_analysis_basic():
    """10 markets at 65c, 8 YES wins → 80% vs 65% expected."""
    markets = [{"final_price": 65, "yes_won": True} for _ in range(8)]
    markets += [{"final_price": 65, "yes_won": False} for _ in range(2)]

    result = bucket_analysis(markets)
    assert len(result) == 1
    b = result[0]
    assert b["bucket"] == "60-70c"
    assert b["count"] == 10
    assert b["wins"] == 8
    assert b["win_rate"] == 80.0
    assert b["expected"] == 65.0
    assert b["edge"] == 15.0


def test_bucket_analysis_multiple_buckets():
    """Markets in different buckets get separate entries."""
    markets = [
        {"final_price": 25, "yes_won": True},
        {"final_price": 75, "yes_won": False},
    ]
    result = bucket_analysis(markets)
    buckets = {b["bucket"]: b for b in result}
    assert "20-30c" in buckets
    assert "70-80c" in buckets


def test_binomial_pvalue_perfectly_calibrated():
    """50 wins out of 100 at 50% → high p-value (not significant)."""
    p = _binomial_pvalue(50, 100, 0.5)
    assert p > 0.05


def test_binomial_pvalue_extreme_deviation():
    """90 wins out of 100 at 50% → tiny p-value."""
    p = _binomial_pvalue(90, 100, 0.5)
    assert p < 0.001


def test_binomial_pvalue_edge_cases():
    """Zero trials → p=1.0, no crash."""
    assert _binomial_pvalue(0, 0, 0.5) == 1.0
    assert _binomial_pvalue(0, 0, 0.0) == 1.0
