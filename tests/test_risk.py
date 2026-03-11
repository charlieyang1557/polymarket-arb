"""Tests for the risk manager."""

import pytest

from src.risk import RiskManager
from src.models import ArbitrageOpportunity, Trade


class TestRiskManager:
    def test_blocks_after_daily_loss_limit(self):
        raise NotImplementedError("Implement in Session 4")

    def test_blocks_after_consecutive_losses(self):
        raise NotImplementedError("Implement in Session 4")

    def test_enforces_max_exposure(self):
        raise NotImplementedError("Implement in Session 4")

    def test_respects_cooldown_after_loss(self):
        raise NotImplementedError("Implement in Session 4")

    def test_position_size_respects_max(self):
        raise NotImplementedError("Implement in Session 4")
