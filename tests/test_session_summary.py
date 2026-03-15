# tests/test_session_summary.py
"""Tests for session summary generation from DB data."""

import sqlite3
import tempfile
import os
from pathlib import Path

from scripts.session_summary import generate_summary, get_session_id, compute_pnl_split


def _create_test_db(path: str, session_id: str = "test-session-001"):
    """Create a test DB with sample data."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE mm_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ticker TEXT, side TEXT, price INTEGER,
            size INTEGER, remaining INTEGER, queue_pos_initial INTEGER,
            status TEXT, placed_at TEXT, filled_at TEXT,
            cancelled_at TEXT, cancel_reason TEXT, time_in_queue_s REAL
        );
        CREATE TABLE mm_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, order_id INTEGER, ticker TEXT, side TEXT,
            price INTEGER, size INTEGER, fee REAL, is_taker INTEGER,
            inventory_after INTEGER, pair_id INTEGER, pair_pnl REAL,
            filled_at TEXT
        );
        CREATE TABLE mm_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, ticker TEXT,
            best_yes_bid INTEGER, yes_ask INTEGER, spread INTEGER,
            midpoint REAL, net_inventory INTEGER, yes_held INTEGER,
            no_held INTEGER, realized_pnl REAL, unrealized_pnl REAL,
            total_pnl REAL, total_fees REAL,
            yes_order_price INTEGER, yes_queue_pos INTEGER,
            no_order_price INTEGER, no_queue_pos INTEGER,
            trade_volume_1min INTEGER,
            global_realized_pnl REAL, global_unrealized_pnl REAL,
            global_total_pnl REAL
        );
        CREATE TABLE mm_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, ticker TEXT, layer INTEGER,
            action TEXT, trigger_reason TEXT,
            net_inventory INTEGER, realized_pnl REAL,
            unrealized_pnl REAL, midpoint REAL, spread INTEGER,
            consecutive_losses INTEGER
        );
    """)

    sid = session_id
    # Insert sample data
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 1, "MKT_A", "yes_bid", 45, 2, 0.77, 0, 2,
         "2026-03-15T10:00:00+00:00"))
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 2, "MKT_A", "no_bid", 53, 2, 0.87, 0, 0,
         "2026-03-15T10:05:00+00:00"))
    # Settlement fill with pair_pnl
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, pair_pnl, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, None, "MKT_A", "settlement", 100, 1, 0, 0, 0, 2.0,
         "2026-03-15T12:00:00+00:00"))

    # Snapshots for duration calc
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T10:00:00+00:00", "MKT_A", 2, 0.0, -1.0, -1.0, 0.77, 8, 47.5))
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T12:00:00+00:00", "MKT_A", 0, 2.36, 0.0, 2.36, 1.64, 8, 48.0))

    # Orders with queue times
    conn.execute(
        "INSERT INTO mm_orders (session_id, ticker, side, price, size, "
        "remaining, queue_pos_initial, status, placed_at, time_in_queue_s) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "MKT_A", "yes", 45, 2, 0, 100, "filled",
         "2026-03-15T09:55:00+00:00", 300.0))

    # Events
    conn.execute(
        "INSERT INTO mm_events (session_id, ts, ticker, layer, action, "
        "trigger_reason) VALUES (?,?,?,?,?,?)",
        (sid, "2026-03-15T11:00:00+00:00", "MKT_A", 4, "PAUSE_60S",
         "spread=2"))
    conn.execute(
        "INSERT INTO mm_events (session_id, ts, ticker, layer, action, "
        "trigger_reason) VALUES (?,?,?,?,?,?)",
        (sid, "2026-03-15T12:00:00+00:00", "MKT_A", 4, "EXIT_MARKET",
         "GAME STARTED"))

    conn.commit()
    conn.close()


class TestSessionSummary:
    def test_generate_summary_has_header(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            _create_test_db(db_path)
            summary = generate_summary(db_path, "test-session-001")
            assert "# Session Summary: test-session-001" in summary
            assert "Duration:" in summary
            assert "Markets:" in summary
        finally:
            os.unlink(db_path)

    def test_generate_summary_has_market_table(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            _create_test_db(db_path)
            summary = generate_summary(db_path, "test-session-001")
            assert "| MKT_A |" in summary
            assert "## Per-Market Results" in summary
        finally:
            os.unlink(db_path)

    def test_generate_summary_has_aggregate_stats(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            _create_test_db(db_path)
            summary = generate_summary(db_path, "test-session-001")
            assert "Total realized P&L:" in summary
            assert "Total fees:" in summary
            assert "Avg queue time to fill: 300s" in summary
        finally:
            os.unlink(db_path)

    def test_generate_summary_has_events(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            _create_test_db(db_path)
            summary = generate_summary(db_path, "test-session-001")
            assert "L4 pauses: 1" in summary
            assert "Game exits: 1" in summary
            assert "Market deactivations: 1" in summary
        finally:
            os.unlink(db_path)

    def test_get_session_id_latest(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            _create_test_db(db_path)
            conn = sqlite3.connect(db_path)
            sid = get_session_id(conn, None)
            conn.close()
            assert sid == "test-session-001"
        finally:
            os.unlink(db_path)

    def test_empty_db_returns_no_data(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE mm_fills (id INTEGER PRIMARY KEY, session_id TEXT, filled_at TEXT);
                CREATE TABLE mm_snapshots (id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT);
                CREATE TABLE mm_events (id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT);
            """)
            conn.close()
            summary = generate_summary(db_path, None)
            assert "No session data found" in summary
        finally:
            os.unlink(db_path)

    def test_generate_summary_has_action_items_section(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            _create_test_db(db_path)
            summary = generate_summary(db_path, "test-session-001")
            assert "## Action Items for Next Session" in summary
            assert "## What Worked" in summary
            assert "## What Failed" in summary
        finally:
            os.unlink(db_path)


def _create_test_db_with_roundtrips(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE mm_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ticker TEXT, side TEXT, price INTEGER, size INTEGER, remaining INTEGER, queue_pos_initial INTEGER, status TEXT, placed_at TEXT, filled_at TEXT, cancelled_at TEXT, cancel_reason TEXT, time_in_queue_s REAL);
        CREATE TABLE mm_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, order_id INTEGER, ticker TEXT, side TEXT, price INTEGER, size INTEGER, fee REAL, is_taker INTEGER, inventory_after INTEGER, pair_id INTEGER, pair_pnl REAL, filled_at TEXT);
        CREATE TABLE mm_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT, ticker TEXT, best_yes_bid INTEGER, yes_ask INTEGER, spread INTEGER, midpoint REAL, net_inventory INTEGER, yes_held INTEGER, no_held INTEGER, realized_pnl REAL, unrealized_pnl REAL, total_pnl REAL, total_fees REAL, yes_order_price INTEGER, yes_queue_pos INTEGER, no_order_price INTEGER, no_queue_pos INTEGER, trade_volume_1min INTEGER, global_realized_pnl REAL, global_unrealized_pnl REAL, global_total_pnl REAL);
        CREATE TABLE mm_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT, ticker TEXT, layer INTEGER, action TEXT, trigger_reason TEXT, net_inventory INTEGER, realized_pnl REAL, unrealized_pnl REAL, midpoint REAL, spread INTEGER, consecutive_losses INTEGER);
    """)
    sid = "test-pnl-split"
    conn.execute("INSERT INTO mm_fills (session_id, order_id, ticker, side, price, size, fee, is_taker, inventory_after, filled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 1, "MKT_A", "yes_bid", 45, 2, 0.77, 0, 2, "2026-03-15T10:00:00+00:00"))
    conn.execute("INSERT INTO mm_fills (session_id, order_id, ticker, side, price, size, fee, is_taker, inventory_after, filled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 2, "MKT_A", "no_bid", 53, 2, 0.87, 0, 0, "2026-03-15T10:05:00+00:00"))
    conn.execute("INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T10:00:00+00:00", "MKT_A", 0, 2.36, 0.0, 2.36, 1.64, 8, 48.0))
    conn.execute("INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T12:00:00+00:00", "MKT_A", 0, 2.36, 0.0, 2.36, 1.64, 8, 48.0))
    conn.commit()
    conn.close()


def _create_test_db_with_residual(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE mm_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ticker TEXT, side TEXT, price INTEGER, size INTEGER, remaining INTEGER, queue_pos_initial INTEGER, status TEXT, placed_at TEXT, filled_at TEXT, cancelled_at TEXT, cancel_reason TEXT, time_in_queue_s REAL);
        CREATE TABLE mm_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, order_id INTEGER, ticker TEXT, side TEXT, price INTEGER, size INTEGER, fee REAL, is_taker INTEGER, inventory_after INTEGER, pair_id INTEGER, pair_pnl REAL, filled_at TEXT);
        CREATE TABLE mm_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT, ticker TEXT, best_yes_bid INTEGER, yes_ask INTEGER, spread INTEGER, midpoint REAL, net_inventory INTEGER, yes_held INTEGER, no_held INTEGER, realized_pnl REAL, unrealized_pnl REAL, total_pnl REAL, total_fees REAL, yes_order_price INTEGER, yes_queue_pos INTEGER, no_order_price INTEGER, no_queue_pos INTEGER, trade_volume_1min INTEGER, global_realized_pnl REAL, global_unrealized_pnl REAL, global_total_pnl REAL);
        CREATE TABLE mm_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT, ticker TEXT, layer INTEGER, action TEXT, trigger_reason TEXT, net_inventory INTEGER, realized_pnl REAL, unrealized_pnl REAL, midpoint REAL, spread INTEGER, consecutive_losses INTEGER);
    """)
    sid = "test-residual"
    conn.execute("INSERT INTO mm_fills (session_id, order_id, ticker, side, price, size, fee, is_taker, inventory_after, filled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 1, "MKT_A", "yes_bid", 45, 2, 0.77, 0, 2, "2026-03-15T10:00:00+00:00"))
    conn.execute("INSERT INTO mm_fills (session_id, order_id, ticker, side, price, size, fee, is_taker, inventory_after, filled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 2, "MKT_A", "no_bid", 53, 2, 0.87, 0, 0, "2026-03-15T10:05:00+00:00"))
    conn.execute("INSERT INTO mm_fills (session_id, order_id, ticker, side, price, size, fee, is_taker, inventory_after, filled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 3, "MKT_A", "no_bid", 61, 2, 0.83, 0, -2, "2026-03-15T10:10:00+00:00"))
    conn.execute("INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T10:00:00+00:00", "MKT_A", -2, 1.53, -5.0, -3.47, 2.47, 8, 48.0))
    conn.execute("INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "2026-03-15T12:00:00+00:00", "MKT_A", -2, 1.53, -5.0, -3.47, 2.47, 8, 48.0))
    conn.commit()
    conn.close()


def test_generate_summary_has_pnl_split():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db_with_roundtrips(db_path)
        summary = generate_summary(db_path, "test-pnl-split")
        assert "Spread P&L" in summary
        assert "Inventory P&L" in summary
    finally:
        os.unlink(db_path)


def test_pnl_split_correct_values():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db_with_roundtrips(db_path)
        summary = generate_summary(db_path, "test-pnl-split")
        assert "Spread P&L" in summary
        # Verify it shows "flat" for residual (inventory was paired off)
        assert "flat" in summary
    finally:
        os.unlink(db_path)


def test_pnl_split_with_residual_inventory():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db_with_residual(db_path)
        summary = generate_summary(db_path, "test-residual")
        assert "Inventory P&L" in summary
        # Should show 2 NO residual
        assert "2 NO" in summary
    finally:
        os.unlink(db_path)
