# tests/test_research_viability.py
"""Tests for Phase 1.3 round-trip viability test.

Pure functions only — no DB, no API.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from scripts.research.adverse_selection_test import (
    half_spread_captured,
    adverse_move,
    classify_informed,
    parse_iso_to_ts,
    nearest_snapshot,
    compute_alpha_tolerance,
    compute_alpha_observed,
    summarize_viability,
)


# ---------- half_spread_captured ----------

def test_half_spread_yes_bid_below_mid():
    """yes_bid at 48c, mid at 50 → captured 2c half-spread."""
    assert half_spread_captured("yes_bid", price_cents=48, midpoint=50.0) == 2.0


def test_half_spread_no_bid_below_mid():
    """no_bid at 48c, mid at 50 means 'no_price' is 50, our bid 48 → 2c captured."""
    # For NO, the implied no_price = 100 - midpoint. We bid 48 for NO.
    # half_spread = no_price - our_bid = (100 - 50) - 48 = 2c
    assert half_spread_captured("no_bid", price_cents=48, midpoint=50.0) == 2.0


def test_half_spread_at_mid_zero():
    assert half_spread_captured("yes_bid", price_cents=50, midpoint=50.0) == 0.0


def test_half_spread_negative_means_paid_through_mid():
    """yes_bid at 52c when mid is 50 → -2c (paid above mid, bad)."""
    assert half_spread_captured("yes_bid", price_cents=52, midpoint=50.0) == -2.0


# ---------- adverse_move ----------

def test_adverse_move_yes_bid_mid_drops_is_adverse():
    """yes_bid: mid was 50, dropped to 47 → adverse move = 3c."""
    assert adverse_move("yes_bid", mid_at_fill=50.0, mid_later=47.0) == 3.0


def test_adverse_move_yes_bid_mid_rises_no_adverse():
    """yes_bid: mid rose, that's GOOD, not adverse."""
    assert adverse_move("yes_bid", mid_at_fill=50.0, mid_later=53.0) == 0.0


def test_adverse_move_no_bid_mid_rises_is_adverse():
    """no_bid: long NO, mid rose → NO price dropped = adverse."""
    assert adverse_move("no_bid", mid_at_fill=50.0, mid_later=53.0) == 3.0


def test_adverse_move_no_bid_mid_drops_no_adverse():
    """no_bid: mid dropped → NO price rose, GOOD."""
    assert adverse_move("no_bid", mid_at_fill=50.0, mid_later=47.0) == 0.0


def test_adverse_move_returns_zero_when_mid_missing():
    assert adverse_move("yes_bid", mid_at_fill=None, mid_later=47.0) == 0.0
    assert adverse_move("yes_bid", mid_at_fill=50.0, mid_later=None) == 0.0


# ---------- classify_informed ----------

def test_classify_informed_short_window_triggers():
    """Adverse 1.5c at 30s → informed (threshold 1c)."""
    assert classify_informed(adv_30s=1.5, adv_2min=0.0, adv_5min=0.0) is True


def test_classify_informed_medium_window_triggers():
    assert classify_informed(adv_30s=0.5, adv_2min=2.5, adv_5min=0.0) is True


def test_classify_informed_long_window_triggers():
    assert classify_informed(adv_30s=0.0, adv_2min=1.5, adv_5min=3.5) is True


def test_classify_informed_below_all_thresholds():
    """0.5/1.0/2.0 → all below thresholds → not informed."""
    assert classify_informed(adv_30s=0.5, adv_2min=1.5, adv_5min=2.5) is False


def test_classify_informed_at_thresholds():
    """≥ thresholds is informed."""
    assert classify_informed(adv_30s=1.0, adv_2min=0.0, adv_5min=0.0) is True


# ---------- parse_iso_to_ts / nearest_snapshot ----------

def test_parse_iso_returns_seconds():
    ts = parse_iso_to_ts("2026-04-01T12:00:00+00:00")
    assert isinstance(ts, float)
    assert ts > 1_000_000_000


def test_nearest_snapshot_finds_closest_before():
    snapshots = [
        {"ts": parse_iso_to_ts("2026-04-01T12:00:00+00:00"), "midpoint": 50.0},
        {"ts": parse_iso_to_ts("2026-04-01T12:00:30+00:00"), "midpoint": 51.0},
        {"ts": parse_iso_to_ts("2026-04-01T12:01:00+00:00"), "midpoint": 52.0},
    ]
    target = parse_iso_to_ts("2026-04-01T12:00:35+00:00")  # 5s after the 12:00:30 snapshot
    s = nearest_snapshot(snapshots, target, max_age_s=30)
    assert s["midpoint"] == 51.0


def test_nearest_snapshot_returns_none_if_too_old():
    snapshots = [
        {"ts": parse_iso_to_ts("2026-04-01T12:00:00+00:00"), "midpoint": 50.0},
    ]
    target = parse_iso_to_ts("2026-04-01T12:05:00+00:00")  # 5min later, too far
    s = nearest_snapshot(snapshots, target, max_age_s=60)
    assert s is None


def test_nearest_snapshot_handles_empty():
    target = parse_iso_to_ts("2026-04-01T12:00:00+00:00")
    s = nearest_snapshot([], target, max_age_s=30)
    assert s is None


# ---------- compute_alpha_tolerance ----------

def test_alpha_tolerance_basic():
    """avg_half_spread = 1.5c, avg_loss_distance = 30c → α_max = 0.05."""
    assert compute_alpha_tolerance(avg_half_spread=1.5,
                                    avg_loss_distance=30) == pytest.approx(0.05)


def test_alpha_tolerance_zero_loss_distance_returns_one():
    """No loss distance — formula ill-defined; report 1.0 (unbounded tolerance)."""
    assert compute_alpha_tolerance(avg_half_spread=1.5,
                                    avg_loss_distance=0) == 1.0


def test_alpha_tolerance_negative_spread_returns_zero():
    """If we paid through mid on average, no informed tolerance possible."""
    assert compute_alpha_tolerance(avg_half_spread=-0.5,
                                    avg_loss_distance=20) == 0.0


# ---------- compute_alpha_observed ----------

def test_compute_alpha_observed_fraction():
    """20 of 100 fills classified informed → 0.20."""
    fills = [{"is_informed": i < 20} for i in range(100)]
    assert compute_alpha_observed(fills) == pytest.approx(0.20)


def test_compute_alpha_observed_empty_returns_zero():
    assert compute_alpha_observed([]) == 0.0


# ---------- summarize_viability ----------

def test_summarize_viability_decision_viable():
    """α_tolerance >> α_observed * 1.5 → viable."""
    fills = []
    # 100 fills, half_spread mean 2c, no informed flow
    for _ in range(100):
        fills.append({"half_spread_cents": 2.0, "is_informed": False,
                      "loss_distance_cents": 30,
                      "side": "yes_bid", "price_cents": 50, "size": 1})
    summary = summarize_viability(fills, margin=1.5)
    # α_tolerance = 2 / 30 ≈ 0.067; α_observed = 0; tolerance >> observed
    assert summary["verdict"] == "viable"


def test_summarize_viability_decision_negative():
    """α_tolerance < α_observed → structurally negative."""
    fills = []
    for i in range(100):
        fills.append({"half_spread_cents": 0.5, "is_informed": i < 20,
                      "loss_distance_cents": 30,
                      "side": "yes_bid", "price_cents": 50, "size": 1})
    summary = summarize_viability(fills, margin=1.5)
    # α_tolerance = 0.5/30 ≈ 0.017; α_observed = 0.20; tolerance < observed
    assert summary["verdict"] == "negative"


def test_summarize_viability_includes_breakdowns():
    fills = [{"half_spread_cents": 2.0, "is_informed": False,
              "loss_distance_cents": 25, "side": "yes_bid",
              "price_cents": 50, "size": 1}]
    summary = summarize_viability(fills)
    assert "alpha_tolerance" in summary
    assert "alpha_observed" in summary
    assert "n_fills" in summary
    assert "mean_half_spread_cents" in summary
