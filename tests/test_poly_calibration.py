# tests/test_poly_calibration.py
"""Tests for Polymarket US calibration study — pure functions only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_calibration import (
    extract_resolved_markets,
    bucket_analysis,
    series_breakdown,
    focus_range_analysis,
    _binomial_pvalue,
)


# --- Helpers ---

def _market(slug="aec-cbb-t1-t2-2026-01-01", question="Team A vs. Team B",
            market_type="moneyline", last_trade_price=0.65,
            settlement=1, series_slug="cbb-2025", category="sports",
            market_sides=None, shares_traded=5000, outcomes=None):
    """Build a market dict matching the SDK shape after enrichment."""
    if market_sides is None:
        market_sides = [
            {"long": True, "description": "Team A"},
            {"long": False, "description": "Team B"},
        ]
    return {
        "slug": slug,
        "question": question,
        "marketType": market_type,
        "category": category,
        "seriesSlug": series_slug,
        "marketSides": market_sides,
        "outcomes": outcomes or '["Team A","Team B"]',
        # Enriched fields (added by our code)
        "_last_trade_price": last_trade_price,
        "_settlement": settlement,
        "_shares_traded": shares_traded,
    }


# --- extract_resolved_markets ---

def test_extract_long_won():
    """settlement=1, lastTrade=0.65 → final_price=65, long_won=True."""
    markets = [_market(settlement=1, last_trade_price=0.65)]
    result = extract_resolved_markets(markets)
    assert len(result) == 1
    assert result[0]["long_won"] is True
    assert result[0]["final_price"] == 65


def test_extract_short_won():
    """settlement=0, lastTrade=0.35 → final_price=35, long_won=False."""
    markets = [_market(settlement=0, last_trade_price=0.35)]
    result = extract_resolved_markets(markets)
    assert len(result) == 1
    assert result[0]["long_won"] is False
    assert result[0]["final_price"] == 35


def test_extract_skips_extreme_prices():
    """lastTradePrice at 0 or 1 → skip."""
    markets = [
        _market(settlement=1, last_trade_price=0.0),
        _market(settlement=1, last_trade_price=1.0),
    ]
    result = extract_resolved_markets(markets)
    assert len(result) == 0


def test_extract_skips_none_settlement():
    """No settlement data → skip."""
    markets = [_market(settlement=None, last_trade_price=0.65)]
    result = extract_resolved_markets(markets)
    assert len(result) == 0


def test_extract_skips_low_volume():
    markets = [_market(shares_traded=10)]
    result = extract_resolved_markets(markets, min_shares=100)
    assert len(result) == 0


def test_extract_preserves_series():
    markets = [_market(series_slug="nba-2025")]
    result = extract_resolved_markets(markets)
    assert result[0]["series_slug"] == "nba-2025"


def test_extract_preserves_market_type():
    markets = [_market(market_type="spreads")]
    result = extract_resolved_markets(markets)
    assert result[0]["market_type"] == "spreads"


# --- bucket_analysis ---

def test_bucket_basic():
    """10 markets at 65c, 8 long wins → 80% vs 65% expected."""
    markets = [{"final_price": 65, "long_won": True} for _ in range(8)]
    markets += [{"final_price": 65, "long_won": False} for _ in range(2)]

    result = bucket_analysis(markets)
    assert len(result) == 1
    b = result[0]
    assert b["bucket"] == "60-70c"
    assert b["count"] == 10
    assert b["wins"] == 8
    assert b["win_rate"] == 80.0
    assert b["expected"] == 65.0
    assert b["edge"] == 15.0


def test_bucket_multiple():
    markets = [
        {"final_price": 25, "long_won": True},
        {"final_price": 75, "long_won": False},
    ]
    result = bucket_analysis(markets)
    buckets = {b["bucket"]: b for b in result}
    assert "20-30c" in buckets
    assert "70-80c" in buckets


# --- series_breakdown ---

def test_series_breakdown():
    markets = [
        {"final_price": 65, "long_won": True, "series_slug": "nba-2025"},
        {"final_price": 65, "long_won": True, "series_slug": "nba-2025"},
        {"final_price": 35, "long_won": False, "series_slug": "cbb-2025"},
    ]
    result = series_breakdown(markets)
    assert "nba-2025" in result
    assert "cbb-2025" in result
    assert result["nba-2025"][0]["count"] == 2


# --- focus_range_analysis ---

def test_focus_range():
    markets = []
    for _ in range(24):
        markets.append({"final_price": 65, "long_won": True,
                        "series_slug": "nba-2025"})
    for _ in range(6):
        markets.append({"final_price": 65, "long_won": False,
                        "series_slug": "nba-2025"})

    result = focus_range_analysis(markets, low=60, high=70)
    assert result["n"] == 30
    assert result["win_rate"] == 80.0
    assert result["expected"] == 65.0


def test_focus_range_empty():
    markets = [{"final_price": 25, "long_won": True, "series_slug": "nba-2025"}]
    result = focus_range_analysis(markets, low=60, high=70)
    assert result["n"] == 0


# --- _binomial_pvalue ---

def test_pvalue_calibrated():
    p = _binomial_pvalue(50, 100, 0.5)
    assert p > 0.05


def test_pvalue_extreme():
    p = _binomial_pvalue(90, 100, 0.5)
    assert p < 0.001


def test_pvalue_edge_cases():
    assert _binomial_pvalue(0, 0, 0.5) == 1.0
