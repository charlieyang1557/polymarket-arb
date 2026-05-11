# tests/test_per_side_telemetry.py
"""Per-side fill telemetry in session summaries.

The May 2026 production diagnosis (fill_asymmetry_diagnosis.md) found
209 yes_bid vs 117 no_bid fills — 1.79x asymmetry that was the primary
driver of losses. Session summaries should now surface per-side stats
so this imbalance is visible at a glance, not buried in aggregate P&L.

Stats per side (yes_bid, no_bid):
  - n_fills: count of mm_fills rows for this side
  - contracts: sum of size
  - mean_price: average fill price (cents)
  - paired_fills: count of fills whose pair_id is non-NULL
  - paired_contracts: sum of size for paired fills
  - win_rate: paired fills with pair_pnl > 0 / total paired fills
  - pair_pnl_sum: sum of pair_pnl for paired fills on this side
                  (note: each pair contributes to BOTH sides' sums, so
                  summing pair_pnl_sum across sides double-counts gross)
  - fees_sum: sum of fee column

Plus aggregate:
  - yes_no_ratio: n_yes_fills / n_no_fills (n_no=0 → math.inf)
"""

import math
import sqlite3
import tempfile

import pytest

from scripts.session_summary import compute_per_side_stats, generate_summary


# -- Helpers ----------------------------------------------------------------

def _make_test_db(path: str, fills: list[dict], session_id: str = "s1"):
    """Create a minimal test DB and insert the given fills."""
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
    for f in fills:
        conn.execute(
            "INSERT INTO mm_fills (session_id, order_id, ticker, side, "
            "price, size, fee, is_taker, inventory_after, pair_id, "
            "pair_pnl, filled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, f.get("order_id"), f.get("ticker", "X"),
             f["side"], f["price"], f["size"],
             f.get("fee", 0.0), f.get("is_taker", 0),
             f.get("inv_after", 0), f.get("pair_id"),
             f.get("pair_pnl"), f.get("filled_at", "2026-03-15T10:00:00+00:00")))
    conn.commit()
    return conn


# -- 1. Basic counts -------------------------------------------------------

def test_basic_yes_no_counts(tmp_path):
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 1},
        {"side": "yes_bid", "price": 47, "size": 1},
        {"side": "no_bid", "price": 53, "size": 1},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["n_fills"] == 2
    assert stats["yes_bid"]["contracts"] == 2
    assert stats["no_bid"]["n_fills"] == 1
    assert stats["no_bid"]["contracts"] == 1
    assert stats["yes_no_ratio"] == 2.0


def test_size_aggregation(tmp_path):
    """contracts is sum of size, not just count."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 3},
        {"side": "yes_bid", "price": 47, "size": 2},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["n_fills"] == 2
    assert stats["yes_bid"]["contracts"] == 5


def test_mean_price_size_weighted(tmp_path):
    """mean_price weights by size (a 2-contract fill counts twice)."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 40, "size": 1},
        {"side": "yes_bid", "price": 50, "size": 3},  # weighted 3x
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    # (40*1 + 50*3) / 4 = 190/4 = 47.5
    assert stats["yes_bid"]["mean_price"] == 47.5


# -- 2. Pair attribution + win rate ---------------------------------------

def test_paired_fills_and_win_rate(tmp_path):
    """win_rate = paired fills with positive pair_pnl / total paired."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        # 3 paired yes_bid fills: 2 wins (+5, +3), 1 loss (-2)
        {"side": "yes_bid", "price": 45, "size": 1,
         "pair_id": 1, "pair_pnl": 5.0},
        {"side": "yes_bid", "price": 46, "size": 1,
         "pair_id": 2, "pair_pnl": 3.0},
        {"side": "yes_bid", "price": 50, "size": 1,
         "pair_id": 3, "pair_pnl": -2.0},
        # 1 unpaired yes_bid
        {"side": "yes_bid", "price": 48, "size": 1},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["paired_fills"] == 3
    assert stats["yes_bid"]["paired_contracts"] == 3
    assert stats["yes_bid"]["win_rate"] == pytest.approx(2 / 3)
    assert stats["yes_bid"]["pair_pnl_sum"] == 6.0  # 5 + 3 + (-2)


def test_win_rate_zero_paired_returns_none(tmp_path):
    """No paired fills → win_rate is None (avoids 0/0)."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 1},
        {"side": "yes_bid", "price": 47, "size": 1},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["paired_fills"] == 0
    assert stats["yes_bid"]["win_rate"] is None


# -- 3. yes/no ratio edge cases -------------------------------------------

def test_ratio_no_no_bid_returns_inf(tmp_path):
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 1},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_no_ratio"] == math.inf


def test_ratio_no_yes_bid_is_zero(tmp_path):
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "no_bid", "price": 55, "size": 1},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_no_ratio"] == 0.0


def test_ratio_empty_session_is_zero(tmp_path):
    """No fills at all — ratio defaults to 0 (not NaN)."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_no_ratio"] == 0.0
    assert stats["yes_bid"]["n_fills"] == 0


# -- 4. Fee aggregation ---------------------------------------------------

def test_fees_summed_per_side(tmp_path):
    """fees_sum tracks total fees per side (negative for Polymarket rebates)."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 1, "fee": -0.125},
        {"side": "yes_bid", "price": 47, "size": 1, "fee": -0.130},
        {"side": "no_bid", "price": 53, "size": 1, "fee": -0.124},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["fees_sum"] == pytest.approx(-0.255)
    assert stats["no_bid"]["fees_sum"] == pytest.approx(-0.124)


# -- 5. Excludes non-bid sides --------------------------------------------

def test_excludes_settlement_and_aggress(tmp_path):
    """Settlement and *_aggress fills should not count toward yes/no_bid."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 1},
        {"side": "settlement", "price": 100, "size": 1},
        {"side": "no_aggress", "price": 55, "size": 1, "is_taker": 1},
    ])
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["n_fills"] == 1
    assert stats["no_bid"]["n_fills"] == 0


# -- 6. Session isolation -------------------------------------------------

def test_stats_isolated_per_session(tmp_path):
    """compute_per_side_stats only counts fills for the requested session."""
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, session_id="s1", fills=[
        {"side": "yes_bid", "price": 45, "size": 1},
    ])
    # Add a row to a different session
    conn.execute(
        "INSERT INTO mm_fills (session_id, order_id, ticker, side, "
        "price, size, fee, is_taker, inventory_after, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("s2", None, "X", "yes_bid", 99, 5, 0, 0, 0,
         "2026-03-15T10:00:00+00:00"))
    conn.commit()
    stats = compute_per_side_stats(conn, "s1")
    conn.close()
    assert stats["yes_bid"]["n_fills"] == 1
    assert stats["yes_bid"]["contracts"] == 1  # not the s2 row's 5


# -- 7. Markdown integration -----------------------------------------------

def test_summary_markdown_includes_per_side_section(tmp_path):
    db = str(tmp_path / "t.db")
    conn = _make_test_db(db, fills=[
        {"side": "yes_bid", "price": 45, "size": 1},
        {"side": "no_bid", "price": 55, "size": 1,
         "pair_id": 1, "pair_pnl": 0.0},
    ])
    # need a snapshot for duration calc
    conn.execute(
        "INSERT INTO mm_snapshots (session_id, ts, ticker, net_inventory, "
        "realized_pnl, unrealized_pnl, total_pnl, total_fees, spread, midpoint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("s1", "2026-03-15T10:00:00+00:00", "X", 0, 0.0, 0.0, 0.0, 0.0, 5, 50.0))
    conn.commit()
    conn.close()

    summary = generate_summary(db, "s1")
    assert "Per-Side Telemetry" in summary
    assert "yes_bid" in summary
    assert "no_bid" in summary
    # Ratio should appear in the section
    assert "ratio" in summary.lower()
