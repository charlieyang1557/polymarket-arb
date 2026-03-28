# tests/test_poly_paper_mm.py
"""Tests for Polymarket US paper MM — verify engine integration."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.poly_client import (
    normalize_orderbook, normalize_bbo, calculate_maker_fee,
)


# Fixture: actual SDK book response from poly_diagnostic.py
LIVE_BOOK = {
    "marketData": {
        "marketSlug": "aec-mlb-pit-nym-2026-03-28",
        "bids": [
            {"px": {"value": "0.350", "currency": "USD"}, "qty": "2554.000"},
            {"px": {"value": "0.360", "currency": "USD"}, "qty": "8576.000"},
            {"px": {"value": "0.370", "currency": "USD"}, "qty": "1123.000"},
        ],
        "offers": [
            {"px": {"value": "0.380", "currency": "USD"}, "qty": "1896.000"},
            {"px": {"value": "0.390", "currency": "USD"}, "qty": "21098.000"},
            {"px": {"value": "0.400", "currency": "USD"}, "qty": "2352.000"},
        ],
    }
}


def test_orderbook_engine_integration():
    """Full pipeline: SDK book → normalize → engine parsing.

    The engine does:
      yes_bids_raw = book_fp.get("yes_dollars", [])
      no_bids_raw = book_fp.get("no_dollars", [])
      yes_bids = [[round(float(p)*100), int(float(q))] for p,q in yes_bids_raw]
      best_yes_bid = yes_bids[-1][0]
      best_no_bid = no_bids[-1][0]
      yes_ask = 100 - best_no_bid
      spread = yes_ask - best_yes_bid
    """
    book = normalize_orderbook(LIVE_BOOK)
    fp = book["orderbook_fp"]

    yes_raw = fp["yes_dollars"]
    no_raw = fp["no_dollars"]

    # Engine parsing
    yes_bids = [[round(float(p) * 100), int(float(q))] for p, q in yes_raw]
    no_bids = [[round(float(p) * 100), int(float(q))] for p, q in no_raw]

    # Sorted ascending — best bid is last
    assert yes_bids[-1][0] == 37  # 0.370 → 37c
    assert yes_bids[-1][1] == 1123

    # NO bids: offers 0.38, 0.39, 0.40 → NO = 0.62, 0.61, 0.60
    # Sorted ascending: 0.60, 0.61, 0.62 → best is 62c (last)
    assert no_bids[-1][0] == 62  # 1 - 0.38 = 0.62 → 62c

    # Engine spread calculation
    best_yes_bid = yes_bids[-1][0]
    best_no_bid = no_bids[-1][0]
    yes_ask = 100 - best_no_bid  # 100 - 62 = 38
    spread = yes_ask - best_yes_bid  # 38 - 37 = 1c

    assert best_yes_bid == 37
    assert best_no_bid == 62
    assert yes_ask == 38
    assert spread == 1


def test_obi_microprice_with_poly_book():
    """OBI microprice works with Poly-normalized book."""
    from src.mm.state import obi_microprice

    book = normalize_orderbook(LIVE_BOOK)
    fp = book["orderbook_fp"]

    yes_bids = [[round(float(p) * 100), int(float(q))]
                for p, q in fp["yes_dollars"]]
    no_bids = [[round(float(p) * 100), int(float(q))]
               for p, q in fp["no_dollars"]]

    best_yes_bid = yes_bids[-1][0]
    best_no_bid = no_bids[-1][0]
    yes_ask = 100 - best_no_bid
    yes_depth = sum(q for _, q in yes_bids)
    no_depth = sum(q for _, q in no_bids)

    mp = obi_microprice(best_yes_bid, yes_ask, yes_depth, no_depth)

    # Should be between bid and ask
    assert best_yes_bid <= mp <= yes_ask
    # With similar depth, should be near midpoint
    assert abs(mp - 37.5) < 0.5


def test_rebate_economics():
    """Verify maker rebate math for session summary."""
    # At midpoint 50c: rebate = 0.125c per contract per side
    rebate = calculate_maker_fee(50, category="sports", count=1)
    assert rebate < 0  # negative = income

    # 10 round-trips at 2 contracts each = 20 fills per side
    fills = 20
    total_rebate = abs(rebate) * fills * 2  # both sides
    assert total_rebate > 0

    # Gross spread capture: 2c * 20 round-trips = 40c
    # Rebate adds: 0.125 * 20 * 2 = 5c
    # Net = 45c
    gross = 2 * 20
    net = gross + total_rebate
    assert net > gross


def test_bbo_for_fill_detection():
    """BBO provides last trade price for fill simulation."""
    bbo = normalize_bbo({
        "marketData": {
            "bestBid": {"value": "0.370", "currency": "USD"},
            "bestAsk": {"value": "0.380", "currency": "USD"},
            "lastTradePx": {"value": "0.375", "currency": "USD"},
            "sharesTraded": "5000.000",
            "openInterest": "3000.000",
        }
    })
    assert bbo["best_bid_cents"] == 37
    assert bbo["best_ask_cents"] == 38
    assert bbo["last_trade_cents"] == 38  # rounds to 38
    assert bbo["shares_traded"] == 5000


def test_empty_book_graceful():
    """Engine handles empty book (market closed) — should not crash."""
    book = normalize_orderbook(None)
    fp = book["orderbook_fp"]
    assert fp["yes_dollars"] == []
    assert fp["no_dollars"] == []

    # Engine checks: if not yes_bids_raw or not no_bids_raw → skip tick
    # This is the graceful deactivation path
