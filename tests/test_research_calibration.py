# tests/test_research_calibration.py
"""Tests for Phase 1.1 calibration analysis on resolved sports markets.

These cover the pure analytical functions in scripts/research/polymarket_calibration.py.
The tests use synthetic market data; no real price_history.json is read here.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import random

import pytest

from scripts.research.polymarket_calibration import (
    parse_categories,
    is_sports_market,
    extract_resolved,
    assign_bucket,
    compute_bucket_stats,
    bootstrap_settle_rate_ci,
    precheck_sample_size,
    bucketize,
    summarize_calibration,
)


def _market(price=0.5, did_yes_win=True, categories=("sports",), volume=1000.0,
            slug="m1", closed_time="2026-01-01 12:00:00+00"):
    """Build a synthetic market dict matching price_history.json shape."""
    return {
        "question": "test",
        "did_yes_win": did_yes_win,
        "volume": volume,
        "closed_time": closed_time,
        "category_keywords": list(categories),
        "price_24h_before": price,
        "price_midlife": price,
        "backtest_price": price,
        "price_source": "24h_before_close",
        "slug": slug,
    }


# ---------- parse_categories ----------

def test_parse_categories_handles_python_list():
    m = {"category_keywords": ["sports", "nba"]}
    assert parse_categories(m) == ["sports", "nba"]


def test_parse_categories_handles_string_repr():
    m = {"category_keywords": "['sports']"}
    assert parse_categories(m) == ["sports"]


def test_parse_categories_handles_missing_key():
    assert parse_categories({}) == []


def test_parse_categories_handles_malformed_string():
    m = {"category_keywords": "not_a_list"}
    assert parse_categories(m) == []


# ---------- is_sports_market ----------

def test_is_sports_market_true():
    assert is_sports_market(_market(categories=("sports",))) is True


def test_is_sports_market_false():
    assert is_sports_market(_market(categories=("politics",))) is False


def test_is_sports_market_multi_category_includes_sports():
    assert is_sports_market(_market(categories=("sports", "nba"))) is True


# ---------- extract_resolved ----------

def test_extract_resolved_excludes_null_settlement():
    markets = [_market(), _market(did_yes_win=None)]
    result = extract_resolved(markets)
    assert len(result) == 1


def test_extract_resolved_excludes_null_price():
    m = _market()
    m["price_24h_before"] = None
    result = extract_resolved([m, _market()])
    assert len(result) == 1


def test_extract_resolved_uses_fallback_price():
    """If price_24h_before is null but price_midlife exists, use it (with note)."""
    m = _market(price=0.45)
    m["price_24h_before"] = None
    m["price_midlife"] = 0.42
    result = extract_resolved([m], allow_fallback=True)
    assert len(result) == 1
    assert result[0]["price"] == 0.42
    assert result[0]["price_source"] == "midlife_fallback"


def test_extract_resolved_no_fallback_by_default():
    m = _market()
    m["price_24h_before"] = None
    result = extract_resolved([m])
    assert len(result) == 0


# ---------- assign_bucket ----------

def test_assign_bucket_5c_inside_band():
    # 30-70c band, 5c bucket size, 0.42 → bucket index 2 (40-45c)
    assert assign_bucket(0.42, low=0.30, high=0.70, size=0.05) == 2


def test_assign_bucket_5c_at_boundary():
    # 0.40 falls at the boundary — should go in [40-45) bucket (lower bound inclusive)
    assert assign_bucket(0.40, low=0.30, high=0.70, size=0.05) == 2


def test_assign_bucket_outside_band_returns_none():
    assert assign_bucket(0.20, low=0.30, high=0.70, size=0.05) is None
    assert assign_bucket(0.80, low=0.30, high=0.70, size=0.05) is None


def test_assign_bucket_at_high_boundary_excluded():
    # 0.70 is the high boundary — exclusive (typical convention)
    assert assign_bucket(0.70, low=0.30, high=0.70, size=0.05) is None


def test_assign_bucket_low_boundary_included():
    assert assign_bucket(0.30, low=0.30, high=0.70, size=0.05) == 0


# ---------- compute_bucket_stats ----------

def test_compute_bucket_stats_basic():
    markets = [
        {"price": 0.41, "settled_yes": True},
        {"price": 0.42, "settled_yes": False},
        {"price": 0.44, "settled_yes": True},
    ]
    stats = compute_bucket_stats(markets)
    assert stats["n"] == 3
    assert stats["wins"] == 2
    assert stats["settle_yes_rate"] == pytest.approx(2 / 3)


def test_compute_bucket_stats_empty():
    stats = compute_bucket_stats([])
    assert stats["n"] == 0
    assert stats["settle_yes_rate"] == 0.0


def test_compute_bucket_stats_includes_mean_price():
    markets = [
        {"price": 0.40, "settled_yes": True},
        {"price": 0.50, "settled_yes": False},
    ]
    stats = compute_bucket_stats(markets)
    assert stats["mean_price"] == pytest.approx(0.45)


# ---------- bootstrap_settle_rate_ci ----------

def test_bootstrap_ci_all_yes_high_lower_bound():
    """All-yes settlements → CI lower bound should be near 1."""
    markets = [{"settled_yes": True} for _ in range(20)]
    lo, hi = bootstrap_settle_rate_ci(markets, n_resample=500, alpha=0.05, seed=42)
    assert lo > 0.85
    assert hi == pytest.approx(1.0)


def test_bootstrap_ci_all_no_low_upper_bound():
    markets = [{"settled_yes": False} for _ in range(20)]
    lo, hi = bootstrap_settle_rate_ci(markets, n_resample=500, alpha=0.05, seed=42)
    assert lo == pytest.approx(0.0)
    assert hi < 0.15


def test_bootstrap_ci_balanced_brackets_half():
    random.seed(0)
    markets = [{"settled_yes": i < 50} for i in range(100)]
    random.shuffle(markets)
    lo, hi = bootstrap_settle_rate_ci(markets, n_resample=1000, alpha=0.05, seed=42)
    # 50/100 → CI roughly [0.40, 0.60]
    assert 0.35 < lo < 0.50
    assert 0.50 < hi < 0.65


def test_bootstrap_ci_empty_returns_zero_one():
    """Empty input → return (0, 1) as widest possible CI rather than error."""
    lo, hi = bootstrap_settle_rate_ci([], n_resample=100, alpha=0.05, seed=42)
    assert lo == 0.0
    assert hi == 1.0


def test_bootstrap_ci_deterministic_with_seed():
    markets = [{"settled_yes": i % 2 == 0} for i in range(50)]
    a = bootstrap_settle_rate_ci(markets, n_resample=200, alpha=0.05, seed=123)
    b = bootstrap_settle_rate_ci(markets, n_resample=200, alpha=0.05, seed=123)
    assert a == b


# ---------- precheck_sample_size ----------

def test_precheck_sufficient_main_band():
    """500+ markets in main band → ok=True, no fallback."""
    markets = [{"price": 0.5, "settled_yes": True} for _ in range(500)]
    result = precheck_sample_size(markets, main_band=(0.30, 0.70), wider_band=(0.25, 0.75))
    assert result["ok"] is True
    assert result["n_main"] == 500
    assert result["fallback"] is None


def test_precheck_insufficient_main_widens():
    """Main band too small but wider band sufficient → ok=True with widen fallback."""
    main_only = [{"price": 0.31, "settled_yes": True} for _ in range(100)]
    wider_extra = [{"price": 0.27, "settled_yes": False} for _ in range(450)]
    result = precheck_sample_size(main_only + wider_extra,
                                   main_band=(0.30, 0.70),
                                   wider_band=(0.25, 0.75),
                                   min_n=500)
    assert result["n_main"] == 100
    assert result["n_wider"] == 550
    assert result["fallback"] == "widen_band"


def test_precheck_both_insufficient_returns_aggregate_only():
    """Even wider band too small → fallback is aggregate-only (no per-bucket per-sport)."""
    markets = [{"price": 0.5, "settled_yes": True} for _ in range(50)]
    result = precheck_sample_size(markets, min_n=500)
    assert result["ok"] is False
    assert result["fallback"] == "aggregate_only"


# ---------- bucketize ----------

def test_bucketize_groups_correctly():
    markets = [
        {"price": 0.31, "settled_yes": True},   # bucket 0 (30-35)
        {"price": 0.34, "settled_yes": False},  # bucket 0
        {"price": 0.42, "settled_yes": True},   # bucket 2 (40-45)
        {"price": 0.80, "settled_yes": True},   # outside band
    ]
    result = bucketize(markets, low=0.30, high=0.70, size=0.05)
    assert 0 in result
    assert 2 in result
    assert len(result[0]) == 2
    assert len(result[2]) == 1
    assert sum(len(v) for v in result.values()) == 3  # 0.80 excluded


# ---------- summarize_calibration ----------

def test_summarize_calibration_max_bias_in_band():
    """Synthetic: in 40-50c bucket, settle rate is 20% (true bias=−25pp from 45c mid)."""
    # Build 100 markets across 30-70c with no bias except in 40-50c
    markets = []
    # 30-40: 25 markets at price 0.35, expected ~35% settle yes; we make it 35% to be calibrated
    for i in range(25):
        price = 0.32 + (i % 3) * 0.01
        settled = i < 9  # ~36% settle yes
        markets.append({"price": price, "settled_yes": settled})
    # 40-50: 50 markets with implied ~45c but only 20% actually settle yes (-25pp bias)
    for i in range(50):
        price = 0.42 + (i % 4) * 0.01
        settled = i < 10  # 20% settle yes
        markets.append({"price": price, "settled_yes": settled})
    # 50-60: 25 markets near calibrated
    for i in range(25):
        price = 0.52 + (i % 3) * 0.01
        settled = i < 14  # 56% settle yes
        markets.append({"price": price, "settled_yes": settled})

    summary = summarize_calibration(markets, low=0.30, high=0.70, size=0.05,
                                    n_resample=300, alpha=0.05, seed=42)
    assert "max_miscalibration_pp" in summary
    # Buckets in 40-50c range should show ~-25pp gap
    assert summary["max_miscalibration_pp"] >= 15  # at least +15pp magnitude observed


def test_summarize_calibration_handles_empty():
    summary = summarize_calibration([], low=0.30, high=0.70, size=0.05,
                                    n_resample=100, alpha=0.05, seed=42)
    assert summary["n_total"] == 0
    assert summary["max_miscalibration_pp"] == 0
