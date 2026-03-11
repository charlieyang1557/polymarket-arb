"""
Opportunity Evaluator

Filters raw scanner output by applying fee calculations, liquidity checks,
and minimum edge requirements before passing to the risk manager.

See blueprint Section 2 (Architecture) for role in the pipeline.
"""

from src.client import PolymarketClient
from src.models import ArbitrageOpportunity
from config.settings import RISK_CONFIG, TRADE_FEE_PCT


class OpportunityEvaluator:
    """Evaluates and filters arbitrage opportunities."""

    def __init__(self, client: PolymarketClient):
        self.client = client

    def evaluate(self, opportunity: ArbitrageOpportunity) -> tuple[bool, str]:
        """
        Determine if an opportunity is worth pursuing.

        Returns:
            (approved: bool, reason: str)
        """
        raise NotImplementedError("Implement in Session 2")

    def _check_liquidity(self, opportunity: ArbitrageOpportunity, trade_size_usd: float) -> tuple[bool, float]:
        """
        Check if enough liquidity exists to fill all legs.

        Returns:
            (has_liquidity: bool, max_fillable_usd: float)
        """
        raise NotImplementedError("Implement in Session 2")

    def _calculate_fees(self, opportunity: ArbitrageOpportunity, trade_size_usd: float) -> float:
        """Calculate total fees for all legs of the trade."""
        raise NotImplementedError("Implement in Session 2")

    def _estimate_slippage(self, opportunity: ArbitrageOpportunity, trade_size_usd: float) -> float:
        """Estimate total slippage cost across all legs."""
        raise NotImplementedError("Implement in Session 2")
