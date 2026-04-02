"""SQLite storage for odds comparison snapshots.

Idempotent: inserting the same (timestamp, slug) pair twice is a no-op.
"""

import sqlite3
from pathlib import Path


class OddsDB:
    """Persistent storage for Pinnacle vs Polymarket odds snapshots."""

    def __init__(self, db_path: str = "data/strategy_a/odds_comparison.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                timestamp TEXT NOT NULL,
                slug TEXT NOT NULL,
                sport TEXT,
                event TEXT,
                game_start TEXT,
                hours_to_game REAL,
                pinnacle_home_prob REAL,
                pinnacle_away_prob REAL,
                pinnacle_raw_home REAL,
                pinnacle_raw_away REAL,
                pinnacle_vig REAL,
                poly_yes_price REAL,
                poly_no_price REAL,
                poly_spread INTEGER,
                delta_home REAL,
                delta_away REAL,
                market_type TEXT,
                PRIMARY KEY (timestamp, slug)
            )
        """)
        self.conn.commit()

    def insert_snapshot(self, data: dict):
        """Insert a snapshot, ignoring duplicates (idempotent)."""
        self.conn.execute("""
            INSERT OR IGNORE INTO odds_snapshots (
                timestamp, slug, sport, event, game_start, hours_to_game,
                pinnacle_home_prob, pinnacle_away_prob,
                pinnacle_raw_home, pinnacle_raw_away, pinnacle_vig,
                poly_yes_price, poly_no_price, poly_spread,
                delta_home, delta_away, market_type
            ) VALUES (
                :timestamp, :slug, :sport, :event, :game_start,
                :hours_to_game,
                :pinnacle_home_prob, :pinnacle_away_prob,
                :pinnacle_raw_home, :pinnacle_raw_away, :pinnacle_vig,
                :poly_yes_price, :poly_no_price, :poly_spread,
                :delta_home, :delta_away, :market_type
            )
        """, data)
        self.conn.commit()

    def get_all(self) -> list[dict]:
        """Return all snapshots as list of dicts."""
        cursor = self.conn.execute(
            "SELECT * FROM odds_snapshots ORDER BY timestamp, slug")
        return [dict(row) for row in cursor.fetchall()]

    def count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM odds_snapshots")
        return cursor.fetchone()[0]

    def close(self):
        self.conn.close()
