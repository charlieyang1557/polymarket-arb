# tests/test_poly_client.py
"""Tests for PolyClient — Polymarket US adapter for the MM engine."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.poly_client import (
    normalize_orderbook,
    normalize_trades,
    calculate_maker_fee,
    normalize_bbo,
)


# ---------------------------------------------------------------------------
# Fixtures matching actual Polymarket US SDK output (from poly_diagnostic.py)
# ---------------------------------------------------------------------------

SAMPLE_BOOK = {
    "marketData": {
        "marketSlug": "aec-nba-lal-bos-2026-03-28",
        "bids": [
            {"px": {"value": "0.550", "currency": "USD"}, "qty": "100.000"},
            {"px": {"value": "0.540", "currency": "USD"}, "qty": "200.000"},
            {"px": {"value": "0.500", "currency": "USD"}, "qty": "500.000"},
        ],
        "offers": [
            {"px": {"value": "0.580", "currency": "USD"}, "qty": "150.000"},
            {"px": {"value": "0.600", "currency": "USD"}, "qty": "300.000"},
            {"px": {"value": "0.650", "currency": "USD"}, "qty": "400.000"},
        ],
    }
}

SAMPLE_BBO = {
    "marketData": {
        "marketSlug": "aec-nba-lal-bos-2026-03-28",
        "currentPx": {"value": "0.565", "currency": "USD"},
        "lastTradePx": {"value": "0.560", "currency": "USD"},
        "settlementPx": {"value": "0.550", "currency": "USD"},
        "sharesTraded": "5000.000",
        "openInterest": "3200.000",
        "bestAsk": {"value": "0.580", "currency": "USD"},
        "bestBid": {"value": "0.550", "currency": "USD"},
        "askDepth": 8,
        "bidDepth": 6,
    }
}


# ---------------------------------------------------------------------------
# normalize_orderbook
# ---------------------------------------------------------------------------

def test_normalize_orderbook_basic():
    """SDK book → engine format: yes_dollars/no_dollars with cent prices."""
    result = normalize_orderbook(SAMPLE_BOOK)

    # Engine expects orderbook_fp.yes_dollars as [[price_dollar_str, qty_str], ...]
    # sorted ascending (lowest first, best bid last)
    yes_bids = result["orderbook_fp"]["yes_dollars"]
    no_bids = result["orderbook_fp"]["no_dollars"]

    # YES bids from SDK bids (long side)
    assert len(yes_bids) >= 1
    # Sorted ascending — best bid (highest) is LAST
    prices = [float(p) for p, _ in yes_bids]
    assert prices == sorted(prices)
    # Best YES bid: 0.55 ($)
    assert float(yes_bids[-1][0]) == 0.55
    assert int(float(yes_bids[-1][1])) == 100

    # NO bids from SDK offers (short side → NO side)
    # offer at 0.58 means someone sells YES at 0.58 → NO bid = 1 - 0.58 = 0.42
    assert len(no_bids) >= 1
    prices_no = [float(p) for p, _ in no_bids]
    assert prices_no == sorted(prices_no)
    # Best NO bid = 1 - best_ask = 1 - 0.58 = 0.42
    best_no = float(no_bids[-1][0])
    assert abs(best_no - 0.42) < 0.001


def test_normalize_orderbook_empty():
    """Empty book returns empty arrays."""
    result = normalize_orderbook({"marketData": {"bids": [], "offers": []}})
    assert result["orderbook_fp"]["yes_dollars"] == []
    assert result["orderbook_fp"]["no_dollars"] == []


def test_normalize_orderbook_none():
    """None input returns empty."""
    result = normalize_orderbook(None)
    assert result["orderbook_fp"]["yes_dollars"] == []
    assert result["orderbook_fp"]["no_dollars"] == []


def test_normalize_orderbook_price_format():
    """Prices are dollar strings matching Kalshi format."""
    result = normalize_orderbook(SAMPLE_BOOK)
    yes_bids = result["orderbook_fp"]["yes_dollars"]
    # Each entry is [price_str, qty_str]
    for price_str, qty_str in yes_bids:
        assert isinstance(price_str, str)
        assert isinstance(qty_str, str)
        # Parseable as float
        float(price_str)
        float(qty_str)


def test_normalize_orderbook_no_side_sorting():
    """NO bids sorted ascending (best NO bid = highest = LAST)."""
    result = normalize_orderbook(SAMPLE_BOOK)
    no_bids = result["orderbook_fp"]["no_dollars"]
    # SDK offers: 0.58, 0.60, 0.65
    # → NO bids: 0.42, 0.40, 0.35
    # Sorted ascending: 0.35, 0.40, 0.42 (best bid 0.42 last)
    prices = [float(p) for p, _ in no_bids]
    assert prices == sorted(prices)
    assert abs(prices[-1] - 0.42) < 0.001  # best NO bid
    assert abs(prices[0] - 0.35) < 0.001   # worst NO bid


# ---------------------------------------------------------------------------
# normalize_bbo
# ---------------------------------------------------------------------------

def test_normalize_bbo():
    """BBO returns key fields the engine can use."""
    result = normalize_bbo(SAMPLE_BBO)
    assert result["best_bid_cents"] == 55
    assert result["best_ask_cents"] == 58
    assert result["last_trade_cents"] == 56
    assert result["shares_traded"] == 5000
    assert result["open_interest"] == 3200


def test_normalize_bbo_none():
    result = normalize_bbo(None)
    assert result["best_bid_cents"] == 0
    assert result["best_ask_cents"] == 0


# ---------------------------------------------------------------------------
# normalize_trades
# ---------------------------------------------------------------------------

def test_normalize_trades_empty():
    """No trades → empty list."""
    result = normalize_trades(None)
    assert result == {"trades": []}


def test_normalize_trades_basic():
    """Placeholder: trade normalization from SDK format."""
    # Polymarket US SDK doesn't have a direct trades endpoint in v0.1.2
    # For now, trades come from BBO or WebSocket
    result = normalize_trades([])
    assert result == {"trades": []}


# ---------------------------------------------------------------------------
# calculate_maker_fee
# ---------------------------------------------------------------------------

def test_maker_fee_sports_is_negative():
    """Sports: maker gets REBATE (negative fee). 25% of taker fee."""
    fee = calculate_maker_fee(50, category="sports")
    # Taker fee ≈ 2% * P * (1-P) * 100 = 2% * 0.5 * 0.5 * 100 = 0.5c per contract
    # Rebate = 25% of 0.5c = 0.125c → returned as negative
    assert fee < 0
    assert abs(fee - (-0.125)) < 0.01


def test_maker_fee_at_extreme_price():
    """Fee is lower at extreme prices (P*(1-P) → 0)."""
    fee_mid = calculate_maker_fee(50, category="sports")
    fee_edge = calculate_maker_fee(90, category="sports")
    # P*(1-P) at 50c = 0.25, at 90c = 0.09
    assert abs(fee_edge) < abs(fee_mid)


def test_maker_fee_geopolitical_is_zero():
    """Geopolitical: fee-free for everyone."""
    fee = calculate_maker_fee(50, category="geopolitical")
    assert fee == 0


def test_maker_fee_per_contract():
    """Fee scales with count."""
    fee1 = calculate_maker_fee(50, category="sports", count=1)
    fee5 = calculate_maker_fee(50, category="sports", count=5)
    assert abs(fee5 / fee1 - 5) < 0.01


def test_maker_fee_boundary():
    """Price at 0 or 100 → fee is 0 (P*(1-P)=0)."""
    assert calculate_maker_fee(0, category="sports") == 0
    assert calculate_maker_fee(100, category="sports") == 0


def test_maker_fee_kalshi_comparison():
    """Kalshi maker fee is POSITIVE. Polymarket is NEGATIVE (rebate).

    Kalshi: ceil(1.75% * P*(1-P) * 100) = ceil(0.4375) = 1c at midpoint
    Polymarket: -0.125c at midpoint (25% rebate on 2% taker fee)

    Net swing per side: 1c + 0.125c = 1.125c advantage for Polymarket.
    """
    kalshi_fee = 0.0175 * 0.5 * 0.5 * 100  # 0.4375c per contract
    poly_fee = calculate_maker_fee(50, category="sports")
    # Poly fee is negative (rebate)
    assert poly_fee < 0
    # Kalshi fee is positive
    assert kalshi_fee > 0
    # Difference per side
    advantage = kalshi_fee - poly_fee  # positive + |negative| = bigger advantage
    assert advantage > 0.5  # at least 0.5c advantage per side
