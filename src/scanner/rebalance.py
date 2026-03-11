"""
Type 1: Multi-Option Rebalance Scanner.

For neg-risk events where all outcomes are mutually exclusive, checks if
sum(best_ask for each YES outcome) < 1.0, which creates a guaranteed profit
by buying one YES share in every outcome.
"""

import logging

from config.settings import RISK_CONFIG, TRADE_FEE_PCT
from src.client import PolymarketClient
from src.models import ArbitrageOpportunity, ArbitrageType, Event, Market, Outcome
from src.scanner.base import BaseScanner

logger = logging.getLogger(__name__)

MIN_PROFIT_PCT = RISK_CONFIG["min_profit_type1_pct"] / 100
MIN_VOLUME_24H = 1000.0  # skip illiquid events


class RebalanceScanner(BaseScanner):
    def __init__(self, client: PolymarketClient):
        self.client = client

    def scan(self, events: list[Event]) -> list[ArbitrageOpportunity]:
        opportunities = []
        for event in events:
            opp = self._check_event(event)
            if opp:
                opportunities.append(opp)
        logger.info("RebalanceScanner: %d opportunity(s) found", len(opportunities))
        return opportunities

    def _check_event(self, event: Event) -> ArbitrageOpportunity | None:
        """Check a single event for Type 1 arbitrage."""
        # Only neg-risk markets are mutually exclusive
        neg_risk_markets = [m for m in event.markets if m.neg_risk and m.active]
        if len(neg_risk_markets) < 2:
            return None

        # Filter for liquidity
        liquid = [m for m in neg_risk_markets if m.volume_24h >= MIN_VOLUME_24H]
        if len(liquid) < 2:
            liquid = neg_risk_markets  # fall back if none pass threshold

        # Collect all YES token IDs
        token_ids = []
        for market in liquid:
            for outcome in market.outcomes:
                if outcome.token_id:
                    token_ids.append(outcome.token_id)

        if not token_ids:
            return None

        # Fetch latest prices
        prices = self.client.get_prices(token_ids)

        # For each market pick the YES token (first token = YES in Polymarket convention)
        total_ask = 0.0
        min_liquidity = float("inf")
        populated_markets: list[Market] = []

        for market in liquid:
            yes_outcomes = [o for o in market.outcomes if o.token_id]
            if not yes_outcomes:
                continue
            yes_token = yes_outcomes[0].token_id
            price_data = prices.get(yes_token, {})
            ask = price_data.get("ask", 0.0)
            if ask <= 0:
                continue  # skip if no ask available

            yes_outcomes[0].best_ask = ask
            yes_outcomes[0].best_bid = price_data.get("bid", 0.0)
            total_ask += ask

            # Check book depth for min_book_depth_usd
            depth = self.client.get_book_depth(yes_token, "BUY", RISK_CONFIG["min_book_depth_usd"])
            if depth["filled_usd"] < RISK_CONFIG["min_book_depth_usd"] * 0.5:
                min_liquidity = min(min_liquidity, depth["filled_usd"])
            else:
                min_liquidity = min(min_liquidity, depth["filled_usd"])

            populated_markets.append(market)

        if not populated_markets or total_ask >= 1.0:
            return None

        gross_profit = 1.0 - total_ask
        total_fees = total_ask * TRADE_FEE_PCT * len(populated_markets)
        net_profit = gross_profit - total_fees
        net_profit_pct = net_profit / total_ask * 100

        if net_profit_pct < MIN_PROFIT_PCT * 100:
            logger.debug(
                "Event '%s': edge %.4f%% below threshold", event.title, net_profit_pct
            )
            return None

        logger.info(
            "OPPORTUNITY [T1] '%s': total_ask=%.4f net_profit=%.4f (%.2f%%)",
            event.title, total_ask, net_profit, net_profit_pct,
        )
        return ArbitrageOpportunity(
            opp_type=ArbitrageType.TYPE1_REBALANCE,
            event_id=event.event_id,
            event_title=event.title,
            markets_involved=populated_markets,
            total_cost=total_ask,
            gross_profit=gross_profit,
            total_fees=total_fees,
            net_profit=net_profit,
            net_profit_pct=net_profit_pct,
            min_liquidity_usd=min_liquidity if min_liquidity != float("inf") else 0.0,
            confidence=1.0,
        )
