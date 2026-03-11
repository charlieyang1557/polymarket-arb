"""
Paper Trader

Simulates trade execution using real market prices and order book depth,
without placing actual orders. Records all hypothetical trades for P&L analysis.

See blueprint Section 9.6 for full implementation spec.
See blueprint Phase 2 for when to use this.
"""

from src.db import Database
from src.models import ArbitrageOpportunity, Trade


class PaperTrader:
    """Simulates trade execution for paper trading (Phase 1-2)."""

    def __init__(self, db: Database):
        self.db = db

    async def execute(self, opportunity: ArbitrageOpportunity, size_usd: float) -> Trade:
        """
        Simulate executing all legs of an arbitrage opportunity.

        Steps:
          1. Fetch current order book for each leg
          2. Simulate fill at realistic prices (walk the book)
          3. Record positions and expected P&L
          4. Save to DB

        Returns:
            Trade with status='open' until market resolves
        """
        raise NotImplementedError("Implement in Session 4")

    async def check_resolutions(self) -> list[Trade]:
        """
        Check open paper trades against resolved markets.
        Closes trades and records actual P&L.
        """
        raise NotImplementedError("Implement in Session 4")

    def get_daily_summary(self) -> dict:
        """Return daily P&L summary for dashboard display."""
        raise NotImplementedError("Implement in Session 4")
