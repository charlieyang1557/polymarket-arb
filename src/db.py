"""
SQLite persistence layer using sqlite-utils.

Tables:
  - opportunities: detected arbitrage opportunities
  - trades: paper and live trades
  - risk_state: key-value store for risk manager state

DB file at data/trades.db (created automatically).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import sqlite_utils

from src.models import ArbitrageOpportunity, RiskStatus, Trade

logger = logging.getLogger(__name__)

DB_PATH = Path("data/trades.db")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite_utils.Database(str(path))
        self._ensure_tables()

    def _ensure_tables(self):
        if "opportunities" not in self.db.table_names():
            self.db["opportunities"].create({
                "id": str,
                "detected_at": str,
                "type": str,
                "event_ids": str,       # JSON list
                "market_ids": str,      # JSON list
                "total_cost": float,
                "expected_profit": float,
                "expected_profit_pct": float,
                "confidence": float,
                "details": str,         # JSON dict
            }, pk="id")

        if "trades" not in self.db.table_names():
            self.db["trades"].create({
                "id": str,
                "opportunity_id": str,
                "side": str,
                "entry_prices": str,    # JSON dict
                "entry_sizes": str,     # JSON dict
                "total_cost": float,
                "status": str,
                "profit": float,
                "opened_at": str,
                "closed_at": str,
            }, pk="id")

        if "risk_state" not in self.db.table_names():
            self.db["risk_state"].create({
                "key": str,
                "value": str,
            }, pk="key")

    # ------------------------------------------------------------------
    # Opportunities
    # ------------------------------------------------------------------

    def save_opportunity(self, opp: ArbitrageOpportunity):
        self.db["opportunities"].insert({
            "id": opp.id,
            "detected_at": opp.detected_at.isoformat(),
            "type": opp.type,
            "event_ids": json.dumps(opp.event_ids),
            "market_ids": json.dumps([m.market_id for m in opp.markets]),
            "total_cost": opp.total_cost,
            "expected_profit": opp.expected_profit,
            "expected_profit_pct": opp.expected_profit_pct,
            "confidence": opp.confidence,
            "details": json.dumps(opp.details),
        }, replace=True)

    def get_all_opportunities(self, since: datetime | None = None) -> list[dict]:
        rows = self.db["opportunities"].rows_where(
            "detected_at >= ?" if since else None,
            [since.isoformat()] if since else [],
            order_by="detected_at desc",
        )
        return list(rows)

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def save_trade(self, trade: Trade):
        self.db["trades"].insert({
            "id": trade.id,
            "opportunity_id": trade.opportunity_id,
            "side": trade.side,
            "entry_prices": json.dumps(trade.entry_prices),
            "entry_sizes": json.dumps(trade.entry_sizes),
            "total_cost": trade.total_cost,
            "status": trade.status,
            "profit": trade.profit,
            "opened_at": trade.opened_at.isoformat(),
            "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
        }, replace=True)

    def get_today_trades(self) -> list[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        return list(self.db["trades"].rows_where(
            "opened_at >= ?", [today], order_by="opened_at desc"
        ))

    def get_open_positions(self) -> list[dict]:
        return list(self.db["trades"].rows_where(
            "status = 'open'", order_by="opened_at desc"
        ))

    def get_daily_pnl(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = list(self.db["trades"].rows_where(
            "opened_at >= ? AND status IN ('won','lost')", [today]
        ))
        return sum(r["profit"] or 0 for r in rows)

    # ------------------------------------------------------------------
    # Risk state (key-value)
    # ------------------------------------------------------------------

    def get_risk_value(self, key: str, default=None):
        row = self.db["risk_state"].get(key) if key in [
            r["key"] for r in self.db["risk_state"].rows
        ] else None
        if row is None:
            return default
        return json.loads(row["value"])

    def set_risk_value(self, key: str, value):
        self.db["risk_state"].insert({"key": key, "value": json.dumps(value)}, replace=True)
