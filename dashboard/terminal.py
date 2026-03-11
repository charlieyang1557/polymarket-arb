"""
Rich Terminal Dashboard

Real-time terminal UI showing live opportunities, recent trades,
portfolio status, and risk metrics.

See blueprint Section 9.7 for full layout spec.
Requires: pip install rich
"""

from rich.console import Console
from rich.layout import Layout
from rich.live import Live

from src.models import ArbitrageOpportunity, RiskStatus


class Dashboard:
    """Real-time Rich terminal dashboard (refreshes every 5 seconds)."""

    def __init__(self):
        self.console = Console()
        self._live: Live | None = None

    def start(self):
        """Start the live display."""
        raise NotImplementedError("Implement in Session 5")

    def stop(self):
        """Stop the live display."""
        raise NotImplementedError("Implement in Session 5")

    def update(
        self,
        opportunities: list[ArbitrageOpportunity],
        risk_status: RiskStatus,
        recent_trades: list | None = None,
    ):
        """Push new data to the dashboard."""
        raise NotImplementedError("Implement in Session 5")

    def _build_layout(self) -> Layout:
        """Build the Rich layout with all panels."""
        raise NotImplementedError("Implement in Session 5")
