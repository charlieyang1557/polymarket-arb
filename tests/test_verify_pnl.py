# tests/test_verify_pnl.py
"""Tests for PnL cross-checker (scripts/verify_pnl.py)."""

import sqlite3
import pytest
from scripts.verify_pnl import verify_pair_pnl, verify_realized_pnl


@pytest.fixture
def db_with_fills(tmp_path):
    """Create an in-memory db with known fills for testing."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE mm_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            order_id INTEGER,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            price INTEGER NOT NULL,
            size INTEGER NOT NULL,
            fee REAL NOT NULL,
            is_taker INTEGER NOT NULL,
            inventory_after INTEGER,
            pair_id INTEGER,
            pair_pnl REAL,
            filled_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn, db_path


def _insert_fill(conn, session_id, ticker, side, price, size, fee,
                 pair_id=None, pair_pnl=None):
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
        "size, fee, is_taker, inventory_after, pair_id, pair_pnl, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, 1, ticker, side, price, size, fee, 0, 0,
         pair_id, pair_pnl, "2026-03-22T00:00:00Z"))
    conn.commit()


def test_pair_pnl_correct(db_with_fills):
    """YES@38 + NO@60, size=2 → gross = (100-38-60)*2 = 4, fees = 0.58+0.84."""
    conn, db_path = db_with_fills
    # Paired round-trip: YES@38 + NO@60, size 2
    yes_fee = 0.58
    no_fee = 0.84
    expected_pnl = (100 - 38 - 60) * 2 - yes_fee - no_fee  # 4 - 1.42 = 2.58
    _insert_fill(conn, "s1", "T1", "yes_bid", 38, 2, yes_fee, pair_id=1, pair_pnl=expected_pnl)
    _insert_fill(conn, "s1", "T1", "no_bid", 60, 2, no_fee, pair_id=1, pair_pnl=expected_pnl)

    errors = verify_pair_pnl(db_path)
    assert errors == []


def test_pair_pnl_wrong(db_with_fills):
    """Detect a pair_pnl value that doesn't match the formula."""
    conn, db_path = db_with_fills
    _insert_fill(conn, "s1", "T1", "yes_bid", 38, 2, 0.58, pair_id=1, pair_pnl=999.0)
    _insert_fill(conn, "s1", "T1", "no_bid", 60, 2, 0.84, pair_id=1, pair_pnl=999.0)

    errors = verify_pair_pnl(db_path)
    assert len(errors) == 1
    assert "pair_id=1" in errors[0]


def test_realized_pnl_correct(db_with_fills):
    """realized_pnl = sum of pair_pnls - unpaired fill fees."""
    conn, db_path = db_with_fills
    # Paired fills
    _insert_fill(conn, "s1", "T1", "yes_bid", 38, 2, 0.58, pair_id=1, pair_pnl=2.58)
    _insert_fill(conn, "s1", "T1", "no_bid", 60, 2, 0.84, pair_id=1, pair_pnl=2.58)
    # Unpaired fill (open inventory)
    _insert_fill(conn, "s1", "T1", "yes_bid", 40, 2, 0.60, pair_id=None, pair_pnl=None)

    errors = verify_realized_pnl(db_path, session_id="s1")
    assert errors == []


def test_realized_pnl_no_fills(db_with_fills):
    """Empty db should produce no errors."""
    _, db_path = db_with_fills
    errors = verify_realized_pnl(db_path, session_id="s1")
    assert errors == []


def test_pair_pnl_multiple_pairs(db_with_fills):
    """Multiple pairs all validated independently."""
    conn, db_path = db_with_fills
    # Pair 1: YES@38 + NO@60, size 2
    _insert_fill(conn, "s1", "T1", "yes_bid", 38, 2, 0.58, pair_id=1, pair_pnl=2.58)
    _insert_fill(conn, "s1", "T1", "no_bid", 60, 2, 0.84, pair_id=1, pair_pnl=2.58)
    # Pair 2: YES@45 + NO@52, size 1 → gross = 3, fees = 0.87+0.87
    fee2 = 0.87
    expected2 = (100 - 45 - 52) * 1 - fee2 - fee2  # 3 - 1.74 = 1.26
    _insert_fill(conn, "s1", "T1", "yes_bid", 45, 1, fee2, pair_id=2, pair_pnl=expected2)
    _insert_fill(conn, "s1", "T1", "no_bid", 52, 1, fee2, pair_id=2, pair_pnl=expected2)

    errors = verify_pair_pnl(db_path)
    assert errors == []
