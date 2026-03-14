# tests/test_obi_microprice.py
"""Tests for OBI (Order Book Imbalance) micro-price calculation."""
from src.mm.state import obi_microprice


# -- Basic formula tests --

def test_obi_symmetric_book():
    """Equal depth on both sides → micro-price equals midpoint."""
    # best_bid=45, best_ask=47, spread=2
    # yes_depth=100, no_depth=100 → p = 45 + 2 * (100 / 200) = 46.0
    fair = obi_microprice(best_bid=45, best_ask=47,
                          yes_depth=100, no_depth=100)
    assert fair == 46.0  # same as midpoint


def test_obi_heavy_no_side():
    """More NO depth → fair price pulled toward ask (higher)."""
    # best_bid=45, best_ask=47, spread=2
    # yes_depth=100, no_depth=300 → p = 45 + 2 * (300 / 400) = 46.5
    fair = obi_microprice(best_bid=45, best_ask=47,
                          yes_depth=100, no_depth=300)
    assert fair == 46.5


def test_obi_heavy_yes_side():
    """More YES depth → fair price pulled toward bid (lower)."""
    # best_bid=45, best_ask=47, spread=2
    # yes_depth=300, no_depth=100 → p = 45 + 2 * (100 / 400) = 45.5
    fair = obi_microprice(best_bid=45, best_ask=47,
                          yes_depth=300, no_depth=100)
    assert fair == 45.5


def test_obi_zero_yes_depth():
    """Zero YES depth → fair price at ask."""
    fair = obi_microprice(best_bid=45, best_ask=47,
                          yes_depth=0, no_depth=100)
    assert fair == 47.0  # all weight toward ask


def test_obi_zero_no_depth():
    """Zero NO depth → fair price at bid."""
    fair = obi_microprice(best_bid=45, best_ask=47,
                          yes_depth=100, no_depth=0)
    assert fair == 45.0  # all weight toward bid


def test_obi_both_zero_depth():
    """Both sides empty → falls back to midpoint."""
    fair = obi_microprice(best_bid=45, best_ask=47,
                          yes_depth=0, no_depth=0)
    assert fair == 46.0  # midpoint fallback


def test_obi_single_cent_spread():
    """Spread=1 → micro-price range is only 1c."""
    # best_bid=50, best_ask=51
    # yes=200, no=800 → p = 50 + 1 * (800/1000) = 50.8
    fair = obi_microprice(best_bid=50, best_ask=51,
                          yes_depth=200, no_depth=800)
    assert fair == 50.8


# -- Real orderbook data from diagnostics --

def test_obi_vs_midpoint_real_political_book():
    """Real KXPRESNOMD-28-GN orderbook: heavily asymmetric.

    YES bids: best=26c, total depth ~206k (dominated by 142K at 26c)
    NO bids:  best=73c, total depth ~466k (dominated by 106K at 72c)
    best_ask = 100 - 73 = 27c, spread = 1c
    Midpoint = 26.5c
    OBI: yes_depth=206k, no_depth=466k
      p = 26 + 1 * (466 / 672) = 26.69c
    OBI shifts fair price toward ask — NO side has more depth,
    meaning YES is slightly underpriced by midpoint.
    """
    # Aggregate depths from real data
    yes_depth = (6299 + 3000 + 5050 + 4555 + 950 + 50 + 4999 + 1000 +
                 250 + 120 + 1050 + 275 + 2679 + 5655 + 8011 + 1192 +
                 2535 + 5933 + 14742 + 142276)
    no_depth = (214 + 357 + 2118 + 1087 + 8309 + 7000 + 5732 + 7363 +
                3772 + 5912 + 4501 + 13809 + 20360 + 47237 + 99453 +
                57603 + 28112 + 5255 + 106322 + 40046)

    best_bid = 26
    best_ask = 27  # 100 - best_no_bid(73)

    midpoint = (best_bid + best_ask) / 2  # 26.5
    obi = obi_microprice(best_bid, best_ask, yes_depth, no_depth)

    # OBI should be higher than midpoint (NO depth > YES depth)
    assert obi > midpoint
    # Should be between bid and ask
    assert best_bid <= obi <= best_ask
    # Verify exact value
    expected = best_bid + 1 * (no_depth / (yes_depth + no_depth))
    assert abs(obi - expected) < 0.001


def test_obi_vs_midpoint_sports_near_50():
    """Sports spread market near 50c — typical MM target.

    Simulated from live data: bid=47, ask=49, spread=2
    YES depth=1000, NO depth=800 (slightly YES-heavy)
    Midpoint = 48.0
    OBI = 47 + 2 * (800/1800) = 47.89
    OBI correctly shows fair price slightly below midpoint.
    """
    fair = obi_microprice(best_bid=47, best_ask=49,
                          yes_depth=1000, no_depth=800)
    midpoint = 48.0
    assert fair < midpoint  # YES-heavy → price pulled toward bid
    assert 47 <= fair <= 49
    expected = 47 + 2 * (800 / 1800)
    assert abs(fair - expected) < 0.001
