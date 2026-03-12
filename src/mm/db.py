# src/mm/db.py
"""SQLite database for the paper market maker."""

import sqlite3
from pathlib import Path


class MMDatabase:
    """Manages the MM-specific SQLite database."""

    def __init__(self, db_path: str, session_id: str):
        self.session_id = session_id
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS mm_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price INTEGER NOT NULL,
                size INTEGER NOT NULL,
                remaining INTEGER NOT NULL,
                queue_pos_initial INTEGER,
                status TEXT NOT NULL,
                placed_at TEXT NOT NULL,
                filled_at TEXT,
                cancelled_at TEXT,
                cancel_reason TEXT,
                time_in_queue_s REAL
            );
            CREATE TABLE IF NOT EXISTS mm_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                order_id INTEGER REFERENCES mm_orders(id),
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
            );
            CREATE TABLE IF NOT EXISTS mm_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                ticker TEXT NOT NULL,
                best_yes_bid INTEGER,
                yes_ask INTEGER,
                spread INTEGER,
                midpoint REAL,
                net_inventory INTEGER,
                yes_held INTEGER,
                no_held INTEGER,
                realized_pnl REAL,
                unrealized_pnl REAL,
                total_pnl REAL,
                total_fees REAL,
                yes_order_price INTEGER,
                yes_queue_pos INTEGER,
                no_order_price INTEGER,
                no_queue_pos INTEGER,
                trade_volume_1min INTEGER,
                global_realized_pnl REAL,
                global_unrealized_pnl REAL,
                global_total_pnl REAL
            );
            CREATE TABLE IF NOT EXISTS mm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                ticker TEXT,
                layer INTEGER NOT NULL,
                action TEXT NOT NULL,
                trigger_reason TEXT NOT NULL,
                net_inventory INTEGER,
                realized_pnl REAL,
                unrealized_pnl REAL,
                midpoint REAL,
                spread INTEGER,
                consecutive_losses INTEGER
            );
        """)
        self.conn.commit()

    def insert_order(self, ticker, side, price, size, remaining,
                     queue_pos_initial, status, placed_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO mm_orders (session_id, ticker, side, price, size, "
            "remaining, queue_pos_initial, status, placed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.session_id, ticker, side, price, size, remaining,
             queue_pos_initial, status, placed_at))
        self.conn.commit()
        return cur.lastrowid

    def update_order(self, order_id: int, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [order_id]
        self.conn.execute(f"UPDATE mm_orders SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def insert_fill(self, order_id, ticker, side, price, size, fee,
                    is_taker, inventory_after, filled_at,
                    pair_id=None, pair_pnl=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO mm_fills (session_id, order_id, ticker, side, price, "
            "size, fee, is_taker, inventory_after, pair_id, pair_pnl, "
            "filled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, order_id, ticker, side, price, size, fee,
             is_taker, inventory_after, pair_id, pair_pnl, filled_at))
        self.conn.commit()
        return cur.lastrowid

    def insert_snapshot(self, **kwargs):
        kwargs["session_id"] = self.session_id
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" * len(kwargs))
        self.conn.execute(
            f"INSERT INTO mm_snapshots ({cols}) VALUES ({placeholders})",
            list(kwargs.values()))
        self.conn.commit()

    def insert_event(self, ts, ticker, layer, action, trigger_reason, **kw):
        self.conn.execute(
            "INSERT INTO mm_events (session_id, ts, ticker, layer, action, "
            "trigger_reason, net_inventory, realized_pnl, unrealized_pnl, "
            "midpoint, spread, consecutive_losses) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, ts, ticker, layer, action, trigger_reason,
             kw.get("net_inventory"), kw.get("realized_pnl"),
             kw.get("unrealized_pnl"), kw.get("midpoint"),
             kw.get("spread"), kw.get("consecutive_losses")))
        self.conn.commit()

    def close(self):
        self.conn.close()
