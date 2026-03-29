# tests/test_mm_risk.py
from datetime import datetime, timezone, timedelta
from src.mm.risk import Action, check_layer1, check_layer2, check_layer3, check_layer4, highest_priority
from src.mm.state import MarketState, SimOrder, GlobalState

# -- MarketState.game_start_utc -----------------------------------------------

def test_market_state_has_game_start_utc():
    """MarketState should accept game_start_utc as a datetime."""
    start = datetime(2026, 3, 21, 1, 25, tzinfo=timezone.utc)
    ms = MarketState(ticker="X", game_start_utc=start)
    assert ms.game_start_utc == start


def test_market_state_game_start_utc_default_none():
    """game_start_utc defaults to None."""
    ms = MarketState(ticker="X")
    assert ms.game_start_utc is None

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

def test_l1_accepts_no_bid():
    # midpoint=28 -> NO ref = 72. Price 70 is within 10% of 72
    assert check_layer1(price=70, size=2, midpoint=28.0, side="no") is None

def test_l1_rejects_no_fat_finger():
    # midpoint=28 -> NO ref = 72. Price 60 is outside 10% of 72 (7.2)
    assert check_layer1(price=60, size=2, midpoint=28.0, side="no") is not None

# Layer 2
def test_l2_continue_under_10():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 5  # net +5
    assert check_layer2(ms) == Action.CONTINUE

def test_l2_aggress_11_to_20():
    """Inv 11-20 → AGGRESS_FLATTEN (continuous skew handles smaller inv)."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 15  # net +15
    assert check_layer2(ms) == Action.AGGRESS_FLATTEN

def test_l2_stop_over_25():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 30  # net +30 > 25 threshold
    assert check_layer2(ms) == Action.STOP_AND_FLATTEN

def test_l2_no_skew_at_small_inventory():
    # net=1 with 3h old position — too small to skew
    ms = MarketState(ticker="X")
    ms.yes_queue = [26]
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=3)
    assert check_layer2(ms) == Action.CONTINUE

def test_l2_aggress_at_2h_old_position():
    """Net=6 with 3h old position → AGGRESS_FLATTEN (threshold raised to >5)."""
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 6  # net > 5 threshold
    ms.oldest_fill_time = datetime.now(timezone.utc) - timedelta(hours=3)
    assert check_layer2(ms) == Action.AGGRESS_FLATTEN

def test_l2_force_close_at_4h_old_position():
    ms = MarketState(ticker="X")
    ms.yes_queue = [26] * 6  # net > 5 threshold
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


def test_l3_pause_30min_resets_consecutive_losses():
    """After PAUSE_30MIN is triggered and applied, consecutive_losses
    must be reset so the bot doesn't loop forever re-triggering pause."""
    gs = GlobalState()
    ms = MarketState(ticker="X", consecutive_losses=4, realized_pnl=-10.0)
    gs.markets["X"] = ms

    # First check triggers PAUSE_30MIN
    action = check_layer3(ms, gs)
    assert action == Action.PAUSE_30MIN

    # Simulate engine applying the pause: it should reset consecutive_losses
    from src.mm.risk import apply_pause_30min
    apply_pause_30min(ms)
    assert ms.consecutive_losses == 0
    assert ms.paused_until is not None

    # After reset, L3 should NOT re-trigger PAUSE_30MIN
    action2 = check_layer3(ms, gs)
    assert action2 == Action.CONTINUE

def test_l3_per_market_exit():
    gs = GlobalState()
    ms = MarketState(ticker="X", realized_pnl=-1100)
    # Add offsetting market so global pnl stays above -500 (avoids FULL_STOP)
    ms2 = MarketState(ticker="Y", realized_pnl=700)
    gs.markets["X"] = ms
    gs.markets["Y"] = ms2
    assert check_layer3(ms, gs) == Action.EXIT_MARKET

def test_l3_drawdown_triple_gate():
    """Drawdown while in NET LOSS should trigger FULL_STOP."""
    gs = GlobalState(peak_total_pnl=200)
    ms = MarketState(ticker="X", realized_pnl=-600)
    gs.markets["X"] = ms
    # peak=200, current=-600, drawdown=800 > 50c, > 5% of 200, AND current < 0
    assert check_layer3(ms, gs) == Action.FULL_STOP

def test_l3_drawdown_no_trigger_while_profitable():
    """FULL_STOP must NOT trigger when total PnL is still positive.

    Regression: rpnl=+69.4c triggered FULL_STOP because drawdown gate
    didn't check whether we were actually losing money.
    """
    gs = GlobalState(peak_total_pnl=200)
    ms = MarketState(ticker="X", realized_pnl=69.4)
    gs.markets["X"] = ms
    # peak=200, current=69.4, drawdown=130.6 > 50, > 5% of 200
    # BUT current > 0 → should NOT trigger FULL_STOP
    assert check_layer3(ms, gs) != Action.FULL_STOP

def test_l3_drawdown_no_trigger_at_plus_100c():
    """Even large drawdown from peak should not stop a profitable session."""
    gs = GlobalState(peak_total_pnl=500)
    ms = MarketState(ticker="X", realized_pnl=100)
    gs.markets["X"] = ms
    # peak=500, current=100, drawdown=400 > 50, > 5%
    # BUT still profitable → no FULL_STOP
    assert check_layer3(ms, gs) != Action.FULL_STOP

def test_l3_daily_loss_triggers_at_negative_600c():
    """FULL_STOP triggers when realized PnL is -600c (below -500 limit)."""
    gs = GlobalState()
    ms = MarketState(ticker="X", realized_pnl=-600)
    gs.markets["X"] = ms
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


# -- Layer 4: Session drift circuit breaker -----------------------------------

def test_l4_drift_exit_at_11c():
    """10c+ drift from session initial midpoint → EXIT_MARKET."""
    ms = MarketState(ticker="X")
    ms.session_initial_midpoint = 50.0
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [(now, 61.0)]  # 11c drift
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.EXIT_MARKET


def test_l4_drift_exit_negative_direction():
    """Drift in negative direction also triggers."""
    ms = MarketState(ticker="X")
    ms.session_initial_midpoint = 50.0
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [(now, 39.0)]  # -11c drift
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.EXIT_MARKET


def test_l4_drift_no_exit_at_9c():
    """9c drift should NOT trigger exit."""
    ms = MarketState(ticker="X")
    ms.session_initial_midpoint = 50.0
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [(now, 59.0)]  # 9c drift
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.CONTINUE


def test_l4_drift_exit_at_exactly_10c():
    """Exactly 10c drift should NOT trigger (> 10, not >=)."""
    ms = MarketState(ticker="X")
    ms.session_initial_midpoint = 50.0
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [(now, 60.0)]  # exactly 10c
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.CONTINUE


def test_l4_drift_no_trigger_without_initial():
    """No session_initial_midpoint set → no drift check."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [(now, 60.0)]
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.CONTINUE


# -- Layer 4: Time-based game start exit --------------------------------------

def test_l4_time_based_exit_at_game_start():
    """Game time reached → EXIT_MARKET."""
    now = datetime.now(timezone.utc)
    ms = MarketState(ticker="X",
                     game_start_utc=now - timedelta(seconds=10))
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.EXIT_MARKET


def test_l4_time_based_soft_close_15min_before():
    """15 min before game → SOFT_CLOSE (reduce-only via flag)."""
    now = datetime.now(timezone.utc)
    ms = MarketState(ticker="X",
                     game_start_utc=now + timedelta(minutes=10))
    action = check_layer4(ms, spread=5, db_error_count=0)
    assert action == Action.SOFT_CLOSE


def test_l4_time_based_no_trigger_2hrs_before():
    """2 hours before game → CONTINUE."""
    now = datetime.now(timezone.utc)
    ms = MarketState(ticker="X",
                     game_start_utc=now + timedelta(hours=2))
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.CONTINUE


def test_l4_frequency_fallback_when_no_schedule():
    """No game_start_utc → time-based check skipped, frequency still works."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    # Simulate >50 trades in 5min for is_live_game
    ms.trade_timestamps = [now - timedelta(seconds=i) for i in range(60)]
    assert ms.is_live_game is True
    # No game_start_utc, so L4 shouldn't trigger EXIT_MARKET from time
    # (live-game exit is handled separately in engine, not L4)
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.CONTINUE
