from abc import ABC, abstractmethod

from src.models import ArbitrageOpportunity, Event


class BaseScanner(ABC):
    @abstractmethod
    def scan(self, events: list[Event]) -> list[ArbitrageOpportunity]:
        """Scan events and return any detected arbitrage opportunities."""
        ...
