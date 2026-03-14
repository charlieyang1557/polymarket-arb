# tests/test_monitor.py
"""Tests for monitor_drain.py false positive fixes."""
import sqlite3
from datetime import datetime, timezone, timedelta

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.monitor_drain import (
    check_drain_v2,
    check_stuck_inventory_v2,
)

SESSION = "test-session"


def _make_db(snapshots: list[dict]) -> sqlite3.Connection:
    """Create in-memory DB with mm_snapshots table and insert rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE mm_snapshots (
            id INTEGER PRIMARY KEY,
            ts TEXT, session_id TEXT, ticker TEXT,
            yes_queue_pos INTEGER, no_queue_pos INTEGER,
            yes_order_price INTEGER, no_order_price INTEGER,
            net_inventory INTEGER, realized_pnl REAL,
            trade_volume_1min INTEGER DEFAULT 0
        )
    """)
    for s in snapshots:
        conn.execute(
            "INSERT INTO mm_snapshots "
            "(ts, session_id, ticker, yes_queue_pos, no_queue_pos, "
            " yes_order_price, no_order_price, "
            " net_inventory, realized_pnl, trade_volume_1min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s["ts"], s.get("session_id", SESSION), s["ticker"],
             s.get("yes_queue_pos"), s.get("no_queue_pos"),
             s.get("yes_order_price"), s.get("no_order_price"),
             s.get("net_inventory", 0), s.get("realized_pnl", 0.0),
             s.get("trade_volume_1min", 0))
        )
    conn.commit()
    return conn


# ============================================================
# Issue 1: DRAIN — hybrid API + snapshot check
# ============================================================

def test_drain_ok_when_queue_pos_decreased():
    """Queue position decreased → drain working, no alert regardless of API."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
        {"ts": t1, "ticker": "X", "yes_queue_pos": 300, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    # API trades at our price — but queue drained, so no alert
    api_trades = [{"yes_price_dollars": "0.21", "count_fp": "10"}]
    alerts = check_drain_v2(conn, SESSION, {"X": api_trades})
    assert len(alerts) == 0


def test_drain_no_alert_api_trades_at_different_price():
    """API has trades at 28c, our order at 21c → no alert."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
        {"ts": t1, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    # 10 trades at 28c — above our 21c bid, shouldn't drain our queue
    api_trades = [{"yes_price_dollars": "0.28", "count_fp": "10"}]
    alerts = check_drain_v2(conn, SESSION, {"X": api_trades})
    assert len(alerts) == 0


def test_drain_alert_api_trades_at_our_price_queue_unchanged():
    """API has 10 trades at 21c, our order at 21c, queue unchanged → ALERT."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
        {"ts": t1, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    # 10 trades at 21c — at our price, should have drained
    api_trades = [{"yes_price_dollars": "0.21", "count_fp": "10"}]
    alerts = check_drain_v2(conn, SESSION, {"X": api_trades})
    assert len(alerts) == 1
    assert "DRAIN" in alerts[0]


def test_drain_alert_api_trades_below_our_price():
    """API trades at 19c, our YES bid at 21c → should drain (price <= bid)."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
        {"ts": t1, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    # Trades at 19c — below our 21c bid, should have drained
    api_trades = [{"yes_price_dollars": "0.19", "count_fp": "10"}]
    alerts = check_drain_v2(conn, SESSION, {"X": api_trades})
    assert len(alerts) == 1
    assert "DRAIN" in alerts[0]


def test_drain_no_side_check_when_no_order_price():
    """No order prices in snapshot (NULL) → no alert even with API trades."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": None, "no_queue_pos": None,
         "yes_order_price": None, "no_order_price": None},
        {"ts": t1, "ticker": "X", "yes_queue_pos": None, "no_queue_pos": None,
         "yes_order_price": None, "no_order_price": None},
    ])
    api_trades = [{"yes_price_dollars": "0.21", "count_fp": "10"}]
    alerts = check_drain_v2(conn, SESSION, {"X": api_trades})
    assert len(alerts) == 0


def test_drain_no_alert_single_snapshot():
    """Only one snapshot — can't compare, no alert."""
    now = datetime.now(timezone.utc)
    conn = _make_db([
        {"ts": now.isoformat(), "ticker": "X",
         "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    alerts = check_drain_v2(conn, SESSION, {"X": []})
    assert len(alerts) == 0


def test_drain_no_alert_no_api_trades():
    """No API trades at all → quiet, no alert."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
        {"ts": t1, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    alerts = check_drain_v2(conn, SESSION, {"X": []})
    assert len(alerts) == 0


def test_drain_checks_no_side_too():
    """API trade at NO price that should drain NO queue → alert if unchanged."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=3)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
        {"ts": t1, "ticker": "X", "yes_queue_pos": 500, "no_queue_pos": 400,
         "yes_order_price": 21, "no_order_price": 55},
    ])
    # Trade at yes_price=45c → no_price=55c, matching our NO bid of 55c
    api_trades = [{"yes_price_dollars": "0.45", "count_fp": "10"}]
    alerts = check_drain_v2(conn, SESSION, {"X": api_trades})
    assert len(alerts) == 1
    assert "DRAIN" in alerts[0]


# ============================================================
# Issue 2: STUCK INVENTORY spam
# ============================================================

def test_stuck_no_alert_under_120min():
    """Inventory held for 90 minutes — under 120min threshold, no alert."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=90)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
        {"ts": t1, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
    ])
    alerts, _ = check_stuck_inventory_v2(conn, SESSION, ["X"], set())
    assert len(alerts) == 0


def test_stuck_alert_over_120min():
    """Inventory stuck for 130 minutes with unchanged pnl → alert."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=130)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
        {"ts": t1, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
    ])
    alerts, already_alerted = check_stuck_inventory_v2(
        conn, SESSION, ["X"], set())
    assert len(alerts) == 1
    assert "STUCK" in alerts[0]
    assert "X" in already_alerted


def test_stuck_no_repeat_alert():
    """Already alerted for this market → don't alert again."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=130)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
        {"ts": t1, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
    ])
    alerts, already_alerted = check_stuck_inventory_v2(
        conn, SESSION, ["X"], {"X"})
    assert len(alerts) == 0


def test_stuck_clears_when_inventory_changes():
    """Inventory changed → clear the already-alerted flag."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=130)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "net_inventory": 3, "realized_pnl": 10.0},
        {"ts": t1, "ticker": "X", "net_inventory": 0, "realized_pnl": 15.0},
    ])
    alerts, already_alerted = check_stuck_inventory_v2(
        conn, SESSION, ["X"], {"X"})
    assert len(alerts) == 0
    assert "X" not in already_alerted


def test_stuck_no_alert_pnl_changed():
    """Inventory non-zero but pnl changed — pairing is happening, no alert."""
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(minutes=130)).isoformat()
    t1 = now.isoformat()
    conn = _make_db([
        {"ts": t0, "ticker": "X", "net_inventory": 5, "realized_pnl": 10.0},
        {"ts": t1, "ticker": "X", "net_inventory": 3, "realized_pnl": 15.0},
    ])
    alerts, _ = check_stuck_inventory_v2(conn, SESSION, ["X"], set())
    assert len(alerts) == 0
