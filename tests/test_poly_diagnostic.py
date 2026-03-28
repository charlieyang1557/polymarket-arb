# tests/test_poly_diagnostic.py
"""Tests for Polymarket US diagnostic — pure functions only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_diagnostic import (
    parse_book,
    compute_spread,
    extract_tags,
)


# --- parse_book ---

def test_parse_book_basic():
    """Parse Polymarket US orderbook format: px.value + qty strings."""
    raw = {
        "marketData": {
            "bids": [
                {"px": {"value": "0.600", "currency": "USD"}, "qty": "100.000"},
                {"px": {"value": "0.580", "currency": "USD"}, "qty": "200.000"},
            ],
            "offers": [
                {"px": {"value": "0.650", "currency": "USD"}, "qty": "150.000"},
                {"px": {"value": "0.680", "currency": "USD"}, "qty": "300.000"},
            ],
        }
    }
    bids, asks = parse_book(raw)
    assert bids == [(0.600, 100.0), (0.580, 200.0)]
    assert asks == [(0.650, 150.0), (0.680, 300.0)]


def test_parse_book_empty():
    bids, asks = parse_book({"marketData": {"bids": [], "offers": []}})
    assert bids == []
    assert asks == []


def test_parse_book_missing():
    bids, asks = parse_book({})
    assert bids == []
    assert asks == []


def test_parse_book_none():
    bids, asks = parse_book(None)
    assert bids == []
    assert asks == []


# --- compute_spread ---

def test_spread_basic():
    bids = [(0.60, 100), (0.58, 200)]
    asks = [(0.65, 150), (0.68, 300)]
    result = compute_spread(bids, asks)
    assert result["best_bid"] == 0.60
    assert result["best_ask"] == 0.65
    assert abs(result["spread"] - 0.05) < 0.001
    assert abs(result["midpoint"] - 0.625) < 0.001
    assert result["bid_depth"] == 300
    assert result["ask_depth"] == 450


def test_spread_empty():
    result = compute_spread([], [])
    assert result["spread"] == 0
    assert result["midpoint"] == 0


def test_spread_one_side():
    result = compute_spread([(0.50, 100)], [])
    assert result["spread"] == 0


# --- extract_tags ---

def test_extract_tags_from_event():
    event = {
        "tags": [
            {"slug": "basketball", "name": "Basketball"},
            {"slug": "nba", "name": "NBA"},
        ],
        "seriesSlug": "nba-2025",
        "category": "sports",
    }
    tags = extract_tags(event)
    assert "basketball" in tags
    assert "nba" in tags


def test_extract_tags_string_list():
    event = {"tags": ["basketball", "sports"], "seriesSlug": "", "category": "sports"}
    tags = extract_tags(event)
    assert "basketball" in tags


def test_extract_tags_includes_series():
    event = {"tags": [], "seriesSlug": "nba-2025", "category": "sports"}
    tags = extract_tags(event)
    assert "nba-2025" in tags


def test_extract_tags_empty():
    event = {"tags": [], "seriesSlug": "", "category": ""}
    tags = extract_tags(event)
    assert tags == []
