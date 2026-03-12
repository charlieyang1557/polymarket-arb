# tests/test_mm_risk.py
from datetime import datetime, timezone, timedelta
from src.mm.risk import Action, check_layer1, check_layer2, check_layer3, check_layer4, highest_priority
from src.mm.state import MarketState, SimOrder, GlobalState

def test_action_priority():
    assert highest_priority([Action.CONTINUE, Action.PAUSE_60S, Action.SKIP_TICK]) == Action.PAUSE_60S
    assert highest_priority([Action.CONTINUE]) == Action.CONTINUE
    assert highest_priority([Action.FULL_STOP, Action.CONTINUE]) == Action.FULL_STOP

# Layer 1
def test_l1_rejects_oversized():
    assert check_layer1(price=26, size=10, midpoint=28.0, max_size=5) is not None

def test_l1_rejects_fat_finger():
    # midpoint=28, 10% = 2.8, so price 32 is outside ±10%
    assert check_layer1(price=32, size=2, midpoint=28.0, max_size=5) is not None

def test_l1_accepts_valid():
    assert check_layer1(price=27, size=2, midpoint=28.0, max_size=5) is None

# Layer 2
def test_l2_continue_under_10():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 5  # net +5
    assert check_layer2(ms) == Action.CONTINUE

def test_l2_skew_11_to_20():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 15  # net +15
    assert check_layer2(ms) == Action.SKEW_QUOTES

def test_l2_aggress_after_skew_1h():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 15  # net +15
    ms.skew_activated_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=1)
    assert check_layer2(ms) == Action.AGGRESS_FLATTEN

def test_l2_stop_over_20():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 25  # net +25
    assert check_layer2(ms) == Action.STOP_AND_FLATTEN

def test_l2_skew_at_2h_old_position():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26]
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=3)
    assert check_layer2(ms) == Action.SKEW_QUOTES

def test_l2_force_close_at_4h_old_position():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26]
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=5)
    assert check_layer2(ms) == Action.FORCE_CLOSE

# Layer 3
def test_l3_daily_loss_full_stop():
    gs = GlobalState()
    ms = MarketState(ticker="X", realized_pnl=-600)  # > $5 loss
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.FULL_STOP

def test_l3_consecutive_losses_pause():
    gs = GlobalState()
    ms = MarketState(ticker="X", consecutive_losses=3)
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.PAUSE_30MIN

def test_l3_per_market_exit():
    gs = GlobalState()
    ms = MarketState(ticker="X", realized_pnl=-1100)
    # Add offsetting market so global pnl stays above -500 (avoids FULL_STOP)
    ms2 = MarketState(ticker="Y", realized_pnl=700)
    gs.markets["X"] = ms
    gs.markets["Y"] = ms2
    assert check_layer3(ms, gs) == Action.EXIT_MARKET

def test_l3_drawdown_triple_gate():
    gs = GlobalState(peak_total_pnl=200)
    ms = MarketState(ticker="X", realized_pnl=80)
    gs.markets["X"] = ms
    # peak=200, current=80, drawdown=120 > 50c, > 5% of 200
    assert check_layer3(ms, gs) == Action.FULL_STOP

def test_l3_drawdown_no_trigger_small_peak():
    gs = GlobalState(peak_total_pnl=50)  # peak < 100, gate 1 fails
    ms = MarketState(ticker="X", realized_pnl=0)
    gs.markets["X"] = ms
    assert check_layer3(ms, gs) == Action.CONTINUE

# Layer 4
def test_l4_price_jump():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [
        (now - timedelta(seconds=60), 26.0),
        (now, 32.0),  # 6c jump
    ]
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.PAUSE_60S

def test_l4_crossed_book():
    ms = MarketState(ticker="X")
    assert check_layer4(ms, spread=-1, db_error_count=0) == Action.SKIP_TICK

def test_l4_db_errors():
    ms = MarketState(ticker="X")
    assert check_layer4(ms, spread=5, db_error_count=10) == Action.FULL_STOP
