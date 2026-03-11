"""
Core data models for the Polymarket arbitrage bot.

All models use Pydantic v2 for validation and serialization.
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Fields confirmed from Gamma/CLOB API responses — see data/diagnostics/2026-03-11_005032/
class Outcome(BaseModel):
    token_id: str
    name: str = ""
    best_ask: float = 0.0
    best_bid: float = 0.0
    volume_24h: float = 0.0


class Market(BaseModel):
    market_id: str
    question: str
    event_id: str
    event_slug: str = ""
    outcomes: list[Outcome] = Field(default_factory=list)
    active: bool = True
    neg_risk: bool = False
    volume_24h: float = 0.0


class Event(BaseModel):
    event_id: str
    title: str
    slug: str = ""
    category: str = ""
    markets: list[Market] = Field(default_factory=list)
    active: bool = True


class OrderBookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    token_id: str
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)


class ArbitrageOpportunity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: Literal["type1_rebalance", "type2_logical"]
    event_ids: list[str]
    markets: list[Market]
    total_cost: float
    expected_profit: float
    expected_profit_pct: float
    confidence: float = Field(ge=0.0, le=1.0)
    detected_at: datetime = Field(default_factory=_utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class Trade(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    side: Literal["paper", "live"]
    entry_prices: dict[str, float] = Field(default_factory=dict)   # token_id -> price
    entry_sizes: dict[str, float] = Field(default_factory=dict)    # token_id -> USD
    total_cost: float = 0.0
    status: Literal["open", "won", "lost", "cancelled"] = "open"
    profit: Optional[float] = None
    opened_at: datetime = Field(default_factory=_utcnow)
    closed_at: Optional[datetime] = None


class RiskStatus(BaseModel):
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    current_exposure: float = 0.0
    consecutive_losses: int = 0
    max_drawdown_pct: float = 0.0
    trades_this_hour: int = 0
    can_trade: bool = True
    blocked_reason: Optional[str] = None
