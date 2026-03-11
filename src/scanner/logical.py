"""
Type 2: Logical/Combinatorial Arbitrage Scanner (Phase A — rule-based).

Detects price inconsistencies between logically related markets,
e.g. P(subset) > P(superset).
"""

import logging
import re

from config.relationships import RELATIONSHIP_RULES
from config.settings import RISK_CONFIG, TRADE_FEE_PCT
from src.client import PolymarketClient
from src.models import ArbitrageOpportunity, Event
from src.scanner.base import BaseScanner

logger = logging.getLogger(__name__)

MIN_PROFIT_PCT = RISK_CONFIG["min_profit_type2_pct"] / 100


class LogicalScanner(BaseScanner):
    def __init__(self, client: PolymarketClient):
        self.client = client

    def scan(self, events: list[Event]) -> list[ArbitrageOpportunity]:
        opportunities = []
        # Group markets by category for intra-category comparisons
        by_category: dict[str, list] = {}
        for event in events:
            cat = event.category or "other"
            by_category.setdefault(cat, []).extend(
                (event, market) for market in event.markets if market.active
            )

        for category, event_markets in by_category.items():
            opps = self._scan_category(category, event_markets)
            opportunities.extend(opps)

        logger.info("LogicalScanner: %d opportunity(s) found", len(opportunities))
        return opportunities

    def _scan_category(self, category: str, event_markets: list) -> list[ArbitrageOpportunity]:
        """Find logical mismatches within a category."""
        opportunities = []
        pairs = self._find_related_pairs(event_markets)
        for (event_a, market_a), (event_b, market_b), relationship in pairs:
            opp = self._evaluate_pair(event_a, market_a, event_b, market_b, relationship)
            if opp:
                opportunities.append(opp)
        return opportunities

    def _find_related_pairs(self, event_markets: list) -> list[tuple]:
        """
        Use rule-based heuristics to identify candidate pairs.
        Returns list of ((event_a, market_a), (event_b, market_b), rule_id).
        """
        pairs = []
        for i, (ev_a, mkt_a) in enumerate(event_markets):
            for ev_b, mkt_b in event_markets[i + 1:]:
                rule = self._match_rule(mkt_a.question, mkt_b.question)
                if rule:
                    pairs.append(((ev_a, mkt_a), (ev_b, mkt_b), rule))
        return pairs

    def _match_rule(self, question_a: str, question_b: str) -> str | None:
        """
        Simple heuristic rule matching.
        Returns matched rule ID or None.
        """
        qa, qb = question_a.lower(), question_b.lower()

        # Temporal: "by June" vs "by December" for same topic
        temporal_months = ["january", "february", "march", "april", "may", "june",
                           "july", "august", "september", "october", "november", "december"]
        for i, month_a in enumerate(temporal_months):
            if month_a in qa:
                for month_b in temporal_months[i + 1:]:
                    if month_b in qb:
                        # Check if same base topic (share 3+ words)
                        words_a = set(re.findall(r"\w+", qa)) - {month_a}
                        words_b = set(re.findall(r"\w+", qb)) - {month_b}
                        if len(words_a & words_b) >= 3:
                            return "before_june_implies_before_december"

        # Threshold: ">100k" vs ">90k" etc. (crude heuristic)
        thresholds_a = re.findall(r">\s*\$?([\d,]+)k?", qa)
        thresholds_b = re.findall(r">\s*\$?([\d,]+)k?", qb)
        if thresholds_a and thresholds_b:
            try:
                val_a = int(thresholds_a[0].replace(",", ""))
                val_b = int(thresholds_b[0].replace(",", ""))
                # same base topic
                words_a = set(re.findall(r"[a-z]+", qa))
                words_b = set(re.findall(r"[a-z]+", qb))
                if len(words_a & words_b) >= 2 and val_a > val_b:
                    return "threshold_higher_implies_lower"
            except ValueError:
                pass

        return None

    def _evaluate_pair(self, event_a, market_a, event_b, market_b, rule_id: str) -> ArbitrageOpportunity | None:
        """
        Given two logically related markets and a rule, check if prices are inconsistent.
        Rule: m_a implies m_b  →  P(m_b) >= P(m_a)
        If P(m_a) > P(m_b), there is an opportunity: buy m_b.
        """
        # Fetch YES prices
        token_a = market_a.outcomes[0].token_id if market_a.outcomes else None
        token_b = market_b.outcomes[0].token_id if market_b.outcomes else None
        if not token_a or not token_b:
            return None

        prices = self.client.get_prices([token_a, token_b])
        ask_a = prices.get(token_a, {}).get("ask", 0.0)
        ask_b = prices.get(token_b, {}).get("ask", 0.0)

        if ask_a <= 0 or ask_b <= 0:
            return None

        # m_a implies m_b: expect ask_b <= ask_a
        # If ask_b > ask_a: buy m_a (cheaper superset)
        edge = ask_b - ask_a
        if edge <= 0:
            return None

        fees = ask_a * TRADE_FEE_PCT
        net_profit = edge - fees
        net_profit_pct = net_profit / ask_a * 100

        if net_profit_pct < MIN_PROFIT_PCT * 100:
            return None

        # Depth check
        depth = self.client.get_book_depth(token_a, "BUY", RISK_CONFIG["min_book_depth_usd"])
        min_liquidity = depth["filled_usd"]

        logger.info(
            "OPPORTUNITY [T2] '%s' vs '%s': edge=%.2f%% rule=%s",
            market_a.question[:50], market_b.question[:50], net_profit_pct, rule_id,
        )
        return ArbitrageOpportunity(
            type="type2_logical",
            event_ids=[event_a.event_id, event_b.event_id],
            markets=[market_a, market_b],
            total_cost=ask_a,
            expected_profit=net_profit,
            expected_profit_pct=net_profit_pct,
            confidence=0.7,  # rule-based match is less certain
            details={
                "gross_profit": edge,
                "total_fees": fees,
                "min_liquidity_usd": min_liquidity,
                "event_title": f"{event_a.title} / {event_b.title}",
                "rule": rule_id,
            },
        )
