"""
Risk Manager

Enforces all position limits, stop-loss rules, and circuit breakers
before any trade is executed (paper or live).

See blueprint Section 6 (Risk Management Rules) for all thresholds.
"""

from src.db import Database
from src.models import ArbitrageOpportunity, RiskStatus, Trade
from config.settings import RISK_CONFIG


class RiskManager:
    """Enforces risk limits and tracks portfolio state."""

    def __init__(self, db: Database):
        self.db = db
        self.config = RISK_CONFIG

    def can_trade(self, opportunity: ArbitrageOpportunity) -> tuple[bool, str]:
        """
        Check all risk rules before allowing a trade.

        Checks (in order):
          1. Daily loss limit not exceeded
          2. Consecutive loss limit not hit
          3. Max drawdown not exceeded
          4. Per-market position limit not exceeded
          5. Total exposure limit not exceeded
          6. Hourly trade rate limit not exceeded
          7. Cooldown period after loss respected
          8. Sufficient order book liquidity

        Returns:
            (approved: bool, reason: str)
        """
        raise NotImplementedError("Implement in Session 4")

    def calculate_position_size(self, opportunity: ArbitrageOpportunity) -> float:
        """
        Determine trade size in USD.

        Rules:
          - Never exceed max_single_trade_usd
          - Scale down if liquidity is thin
          - Scale down as limits are approached
          - Optional: half-Kelly criterion

        Returns:
            size_usd: float
        """
        raise NotImplementedError("Implement in Session 4")

    def record_trade_result(self, trade: Trade) -> None:
        """
        Update P&L tracking and check circuit breakers after a trade closes.
        Persists state to DB so it survives restarts.
        """
        raise NotImplementedError("Implement in Session 4")

    def get_status(self) -> RiskStatus:
        """Return current risk status snapshot."""
        raise NotImplementedError("Implement in Session 4")
