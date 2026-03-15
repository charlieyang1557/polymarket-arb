# tests/test_mm_v2.py
"""Tests for Sports MM v2: live-game detection, mode-aware quoting."""
from datetime import datetime, timezone, timedelta
from src.mm.state import MarketState, GlobalState
from src.mm.risk import Action, check_layer4


# -- Live-game detection via trade frequency --

def test_is_live_game_below_threshold():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    # 10 trades in last 5 min — pre-game
    ms.trade_timestamps = [now - timedelta(seconds=i * 30) for i in range(10)]
    assert ms.is_live_game is False


def test_is_live_game_above_threshold():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    # 60 trades in last 5 min — live game
    ms.trade_timestamps = [now - timedelta(seconds=i * 5) for i in range(60)]
    assert ms.is_live_game is True


def test_is_live_game_ignores_old_trades():
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    # 100 trades but all > 10 min ago — not live
    ms.trade_timestamps = [now - timedelta(minutes=15, seconds=i) for i in range(100)]
    assert ms.is_live_game is False


def test_is_live_game_empty():
    ms = MarketState(ticker="X")
    ms.trade_timestamps = []
    assert ms.is_live_game is False


# -- L4 tighter threshold in live-game mode --

def test_l4_price_jump_pregame_5c_threshold():
    """Pre-game: 4c move in 60s does NOT trigger pause."""
    ms = MarketState(ticker="X")
    ms.trade_timestamps = []  # pre-game
    now = datetime.now(timezone.utc)
    ms.midpoint_history = [
        (now - timedelta(seconds=50), 48.0),
        (now, 52.0),  # 4c move
    ]
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.CONTINUE


def test_l4_price_jump_live_game_3c_threshold():
    """Live-game: 4c move in 60s DOES trigger pause (tighter threshold)."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 3) for i in range(60)]
    ms.midpoint_history = [
        (now - timedelta(seconds=50), 48.0),
        (now, 52.0),  # 4c move — above 3c live-game threshold
    ]
    assert check_layer4(ms, spread=5, db_error_count=0) == Action.PAUSE_60S


# -- Post-fill cooldown in live-game mode --

def test_post_fill_cooldown_live_game():
    """After a fill in live-game mode, should set 30s cooldown."""
    ms = MarketState(ticker="X")
    now = datetime.now(timezone.utc)
    ms.trade_timestamps = [now - timedelta(seconds=i * 3) for i in range(60)]
    assert ms.is_live_game is True
    assert ms.post_fill_cooldown_s == 30


def test_post_fill_cooldown_pregame():
    """Pre-game: no post-fill cooldown."""
    ms = MarketState(ticker="X")
    ms.trade_timestamps = []
    assert ms.post_fill_cooldown_s == 0
