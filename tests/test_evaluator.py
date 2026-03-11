"""Tests for the opportunity evaluator."""

import pytest

from src.evaluator import OpportunityEvaluator
from src.models import ArbitrageOpportunity


class TestOpportunityEvaluator:
    def test_rejects_below_min_profit(self):
        raise NotImplementedError("Implement in Session 2")

    def test_rejects_insufficient_liquidity(self):
        raise NotImplementedError("Implement in Session 2")

    def test_approves_valid_opportunity(self):
        raise NotImplementedError("Implement in Session 2")

    def test_calculates_correct_fees(self):
        raise NotImplementedError("Implement in Session 2")
