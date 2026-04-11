# tests/test_mm_state.py
from datetime import datetime, timezone, timedelta
from src.mm.state import MarketState, maker_fee_cents, taker_fee_cents, unrealized_pnl_cents

def test_maker_fee_at_26c():
    # Spec worked example: 2 contracts at 26c = 0.67c
    assert abs(maker_fee_cents(26, 2) - 0.6734) < 0.01

def test_maker_fee_at_69c():
    # Spec: 1 contract at 69c = 0.37c
    assert abs(maker_fee_cents(69, 1) - 0.3745) < 0.01

def test_taker_fee_at_26c():
    # Spec: 2 contracts at 26c = 2.69c
    assert abs(taker_fee_cents(26, 2) - 2.6936) < 0.01

def test_maker_fee_at_50c_maximum():
    # Max fee at P=0.50: 0.0175 * 1 * 0.5 * 0.5 * 100 = 0.4375c
    assert abs(maker_fee_cents(50, 1) - 0.4375) < 0.01

def test_unrealized_pnl_long_yes():
    # Holding 2 YES at costs [26, 28], best_yes_bid=29
    # Unrealized = (29-26) + (29-28) = 4 (conservative: bid not midpoint)
    yes_q = [26, 28]
    no_q = []
    assert unrealized_pnl_cents(yes_q, no_q, best_yes_bid=29, best_no_bid=69) == 4.0

def test_unrealized_pnl_long_no():
    # Holding 1 NO at cost [69], best_no_bid=70
    # Unrealized = 70-69 = 1
    yes_q = []
    no_q = [69]
    assert unrealized_pnl_cents(yes_q, no_q, best_yes_bid=26, best_no_bid=70) == 1.0

def test_unrealized_pnl_hedged():
    # Fully hedged: 2 YES + 2 NO, no unhedged tail
    assert unrealized_pnl_cents([26, 28], [69, 71],
                                best_yes_bid=29, best_no_bid=70) == 0.0

def test_unrealized_pnl_partial_hedge():
    # 3 YES + 1 NO: first pair hedged, 2 YES unhedged
    # Unhedged YES at costs [28, 30], best_yes_bid=31
    # Unrealized = (31-28) + (31-30) = 4
    assert unrealized_pnl_cents([26, 28, 30], [69],
                                best_yes_bid=31, best_no_bid=69) == 4.0


# -- Soft-close tests --

def test_is_soft_close_below_threshold():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 10) for i in range(25)]
    assert ms.is_soft_close is False

def test_is_soft_close_at_threshold():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 5) for i in range(35)]
    assert ms.is_soft_close is True

def test_is_soft_close_not_live_game():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 5) for i in range(40)]
    assert ms.is_soft_close is True
    assert ms.is_live_game is False

def test_is_soft_close_false_when_live():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 3) for i in range(60)]
    assert ms.is_live_game is True
    assert ms.is_soft_close is False

def test_is_soft_close_empty():
    ms = MarketState(ticker="X")
    assert ms.is_soft_close is False


# -- Fair-value anchoring tests (Task 1) --
from src.mm.state import skewed_quotes

def test_skewed_quotes_flat_anchors_to_fair():
    """With no skew, quotes are centered on fair value."""
    # fair=52, spread=4 (best_yes_bid=48, best_no_bid=48 → yes_ask=52)
    # half_spread=4//2=2, yes_price=52-2=50, no_price=(100-52)-2=46
    yes_p, no_p = skewed_quotes(fair=52.0, best_yes_bid=48, best_no_bid=48,
                                 net_inventory=0, gamma=0.5)
    assert yes_p == 50
    assert no_p == 46

def test_skewed_quotes_fair_above_mid_raises_yes_bid():
    """OBI fair above midpoint → YES bid is above midpoint."""
    # fair=53, spread=4 → half=2 → yes_price=51, no_price=45
    yes_p, no_p = skewed_quotes(fair=53.0, best_yes_bid=48, best_no_bid=48,
                                 net_inventory=0)
    assert yes_p == 51
    assert no_p == 45

def test_skewed_quotes_skew_symmetric():
    """Positive inventory skews YES down, NO up by equal amount."""
    yes_p_flat, no_p_flat = skewed_quotes(fair=50.0, best_yes_bid=48,
                                           best_no_bid=48, net_inventory=0)
    yes_p_long, no_p_long = skewed_quotes(fair=50.0, best_yes_bid=48,
                                           best_no_bid=48, net_inventory=2,
                                           gamma=1.0)
    # skew_raw = 2*1.0 = 2. YES down by 2, NO up by 2.
    assert yes_p_long == yes_p_flat - 2
    assert no_p_long == no_p_flat + 2

def test_skewed_quotes_polymarket_floor_gross_1c():
    """Quotes always produce >= 1c gross (Polymarket floor, not Kalshi fee)."""
    yes_p, no_p = skewed_quotes(fair=50.0, best_yes_bid=49, best_no_bid=49,
                                 net_inventory=0, gamma=0.5)
    assert 100 - yes_p - no_p >= 1

def test_skewed_quotes_floor_clamps_extreme_skew():
    """Very large inventory skew gets clamped by profitability floor."""
    yes_p, no_p = skewed_quotes(fair=50.0, best_yes_bid=48, best_no_bid=48,
                                 net_inventory=100, gamma=1.0)
    assert 100 - yes_p - no_p >= 1
    assert yes_p >= 1
    assert no_p >= 1
