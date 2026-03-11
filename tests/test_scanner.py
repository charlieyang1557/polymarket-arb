"""Tests for Type 1 and Type 2 scanners."""

import pytest
from unittest.mock import MagicMock, patch

from src.models import Event, Market, Outcome, ArbitrageOpportunity
from src.scanner.rebalance import RebalanceScanner
from src.scanner.logical import LogicalScanner


class TestRebalanceScanner:
    def test_detects_opportunity_when_sum_below_one(self):
        raise NotImplementedError("Implement in Session 2")

    def test_no_opportunity_when_sum_above_one(self):
        raise NotImplementedError("Implement in Session 2")

    def test_skips_non_neg_risk_events(self):
        raise NotImplementedError("Implement in Session 2")

    def test_accounts_for_fees(self):
        raise NotImplementedError("Implement in Session 2")

    def test_checks_order_book_depth(self):
        raise NotImplementedError("Implement in Session 2")


class TestLogicalScanner:
    def test_detects_temporal_mispricing(self):
        raise NotImplementedError("Implement in Session 3")

    def test_detects_threshold_mispricing(self):
        raise NotImplementedError("Implement in Session 3")

    def test_no_false_positive_on_unrelated_markets(self):
        raise NotImplementedError("Implement in Session 3")
