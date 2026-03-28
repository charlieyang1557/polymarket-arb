# tests/test_poly_spread_scan.py
"""Tests for Polymarket US live spread scanner — pure functions only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_full_scan import (
    parse_book,
    compute_spread,
    compute_net_spread,
    extract_scannable_markets,
    build_series_summary,
)


# --- parse_book (same as diagnostic) ---

def test_parse_book():
    raw = {
        "marketData": {
            "bids": [
                {"px": {"value": "0.600", "currency": "USD"}, "qty": "100.000"},
            ],
            "offers": [
                {"px": {"value": "0.650", "currency": "USD"}, "qty": "150.000"},
            ],
        }
    }
    bids, asks = parse_book(raw)
    assert bids == [(0.600, 100.0)]
    assert asks == [(0.650, 150.0)]


# --- compute_spread ---

def test_spread():
    bids = [(0.60, 100), (0.58, 200)]
    asks = [(0.65, 150), (0.68, 300)]
    r = compute_spread(bids, asks)
    assert r["best_bid"] == 0.60
    assert r["best_ask"] == 0.65
    assert abs(r["spread_cents"] - 5.0) < 0.1
    assert abs(r["midpoint"] - 0.625) < 0.01
    assert r["bid_depth"] == 300
    assert r["ask_depth"] == 450
    assert abs(r["symmetry"] - (300 / 450)) < 0.01


def test_spread_empty():
    r = compute_spread([], [])
    assert r["spread_cents"] == 0


# --- compute_net_spread ---

def test_net_spread_with_rebate():
    """Sports: 25% taker fee rebate to makers. Taker fee = 2% of contracts."""
    # spread = 5c, midpoint = 0.50
    # Taker fee per side ≈ 2c (simplified)
    # Maker rebate = 25% of taker fee = 0.5c per side
    # net_spread = spread + 2*rebate - some taker cost
    # Actually: maker gets: spread capture + rebate on fills
    # net_spread = gross_spread + maker_rebate_per_round_trip
    result = compute_net_spread(spread_cents=5.0, midpoint=0.50,
                                 rebate_pct=0.25, taker_fee_pct=0.02)
    assert result > 5.0  # Rebate adds to maker profit


def test_net_spread_fee_free():
    """Geopolitical: fee-free. net_spread = gross_spread."""
    result = compute_net_spread(spread_cents=5.0, midpoint=0.50,
                                 rebate_pct=0, taker_fee_pct=0)
    assert result == 5.0


def test_net_spread_zero_spread():
    result = compute_net_spread(spread_cents=0, midpoint=0.50,
                                 rebate_pct=0.25, taker_fee_pct=0.02)
    assert result == 0


# --- extract_scannable_markets ---

def _active_market(slug="aec-nba-lal-bos-2026-03-28", question="Lakers vs Celtics",
                   active=True, closed=False, market_type="moneyline",
                   series_slug="nba-2025", shares_traded=1000):
    return {
        "slug": slug,
        "question": question,
        "active": active,
        "closed": closed,
        "marketType": market_type,
        "seriesSlug": series_slug,
        "category": "sports",
        "marketSides": [
            {"long": True, "description": "Lakers"},
            {"long": False, "description": "Celtics"},
        ],
        "_shares_traded": shares_traded,
    }


def test_extract_scannable_basic():
    markets = [_active_market(shares_traded=500)]
    result = extract_scannable_markets(markets, min_shares=100)
    assert len(result) == 1


def test_extract_scannable_skips_closed():
    markets = [_active_market(closed=True)]
    result = extract_scannable_markets(markets)
    assert len(result) == 0


def test_extract_scannable_skips_inactive():
    markets = [_active_market(active=False)]
    result = extract_scannable_markets(markets)
    assert len(result) == 0


def test_extract_scannable_skips_low_volume():
    markets = [_active_market(shares_traded=5)]
    result = extract_scannable_markets(markets, min_shares=100)
    assert len(result) == 0


# --- build_series_summary ---

def test_series_summary():
    candidates = [
        {"series_slug": "nba-2025", "spread_cents": 3.0, "midpoint": 0.50,
         "bid_depth": 500, "ask_depth": 600, "symmetry": 0.83,
         "net_spread_cents": 3.5},
        {"series_slug": "nba-2025", "spread_cents": 5.0, "midpoint": 0.60,
         "bid_depth": 300, "ask_depth": 400, "symmetry": 0.75,
         "net_spread_cents": 5.5},
        {"series_slug": "cbb-2025", "spread_cents": 8.0, "midpoint": 0.45,
         "bid_depth": 100, "ask_depth": 100, "symmetry": 1.0,
         "net_spread_cents": 8.5},
    ]
    result = build_series_summary(candidates)
    by_s = {r["series"]: r for r in result}
    assert "nba-2025" in by_s
    assert by_s["nba-2025"]["markets"] == 2
    assert abs(by_s["nba-2025"]["avg_spread_cents"] - 4.0) < 0.1
    assert "cbb-2025" in by_s


def test_series_summary_empty():
    assert build_series_summary([]) == []
