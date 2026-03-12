# tests/test_mm_db.py
import os, tempfile
from src.mm.db import MMDatabase

def test_create_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        # Tables should exist
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {t[0] for t in tables}
        assert "mm_orders" in names
        assert "mm_fills" in names
        assert "mm_snapshots" in names
        assert "mm_events" in names
        db.close()
    finally:
        os.unlink(path)

def test_insert_order():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        oid = db.insert_order("KXTEST", "yes", 26, 2, 2, 42, "resting",
                              "2026-03-12T00:00:00Z")
        assert oid > 0
        row = db.conn.execute("SELECT * FROM mm_orders WHERE id=?",
                              (oid,)).fetchone()
        assert row is not None
        db.close()
    finally:
        os.unlink(path)

def test_insert_fill():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        fid = db.insert_fill(order_id=None, ticker="KXTEST", side="yes_bid",
                             price=26, size=2, fee=0.67, is_taker=0,
                             inventory_after=2, filled_at="2026-03-12T00:01:00Z")
        assert fid > 0
        db.close()
    finally:
        os.unlink(path)

def test_insert_event():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        db = MMDatabase(path, session_id="test-001")
        db.insert_event("2026-03-12T00:00:00Z", "KXTEST", 2,
                        "AGGRESS_FLATTEN", "net_inv=15 > 10",
                        net_inventory=15, realized_pnl=0,
                        unrealized_pnl=-5.0, midpoint=28.0,
                        spread=5, consecutive_losses=0)
        rows = db.conn.execute("SELECT * FROM mm_events").fetchall()
        assert len(rows) == 1
        db.close()
    finally:
        os.unlink(path)
