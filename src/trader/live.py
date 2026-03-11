"""
Live Trader

Places real orders via py-clob-client on Polygon mainnet.
Only used in Phase 3+ after paper trading validates the strategy.

See blueprint Section 9.8 and Phase 3 for full implementation spec.

IMPORTANT: Requires POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS in .env
"""

from src.db import Database
from src.models import ArbitrageOpportunity, Trade


class LiveTrader:
    """Places real orders on Polymarket (Phase 3+)."""

    def __init__(self, db: Database, private_key: str, funder_address: str, chain_id: int = 137):
        self.db = db
        self._private_key = private_key
        self._funder_address = funder_address
        self._chain_id = chain_id
        self._client = None  # initialized on first use

    def _init_clob_client(self):
        """Lazy-initialize authenticated py-clob-client."""
        raise NotImplementedError("Implement in Phase 3")

    async def execute(self, opportunity: ArbitrageOpportunity, size_usd: float) -> Trade:
        """
        Place real limit orders for all legs of an arbitrage opportunity.

        Steps:
          1. Validate all legs can still be filled at target prices
          2. Place limit orders simultaneously (maker = 0 fee)
          3. Monitor for fills (timeout + cancel if partial)
          4. Record trade with actual fill prices

        Returns:
            Trade with actual entry prices and sizes
        """
        raise NotImplementedError("Implement in Phase 3")

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        raise NotImplementedError("Implement in Phase 3")

    async def get_open_orders(self) -> list[dict]:
        """Fetch all currently open orders from CLOB."""
        raise NotImplementedError("Implement in Phase 3")
