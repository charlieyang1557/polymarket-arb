# tests/test_poly_daily_scan.py
"""Tests for Polymarket US daily scanner — pure functions only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone

from scripts.poly_daily_scan import (
    poly_net_spread_cents,
    apply_prefilters,
    rank_candidates,
    avg_rank,
    is_sport_market,
)


# --- poly_net_spread_cents ---

def test_net_spread_with_rebate():
    """Net spread = gross + maker rebate income (rebate is positive addition)."""
    # spread=5c, midpoint=50c
    # Kalshi: net = 5 - 2*ceil(0.0175*0.5*0.5*100) = 5 - 2*1 = 3c
    # Polymarket: rebate per side = 0.25 * 0.02 * 0.5*0.5 * 100 = 0.125c
    # net = 5 + 2*0.125 = 5.25c
    result = poly_net_spread_cents(5, 50)
    assert result > 5.0  # rebate adds to net spread
    assert abs(result - 5.25) < 0.01


def test_net_spread_zero_spread():
    assert poly_net_spread_cents(0, 50) == 0


def test_net_spread_at_extreme():
    """At midpoint 90c, P*(1-P) is small → small rebate."""
    result = poly_net_spread_cents(5, 90)
    # rebate = 2 * 0.25 * 0.02 * 0.9 * 0.1 * 100 = 0.09c
    assert abs(result - 5.09) < 0.01


# --- is_sport_market (whitelist filter) ---

def test_is_sport_market_traditional_sports():
    """Standard sport slugs pass the whitelist."""
    assert is_sport_market("tsc-mlb-az-tex-2026-05-11-7pt5") is True
    assert is_sport_market("tsc-nba-okc-lal-2026-05-11-216pt5") is True
    assert is_sport_market("tsc-nhl-col-min-2026-05-11-6pt5") is True
    assert is_sport_market("aec-mlb-pit-nym-2026-03-28") is True
    assert is_sport_market("aec-nhl-van-cgy-2026-03-28") is True
    assert is_sport_market("aec-cbb-bayl-minnst-2026-04-01") is True


def test_is_sport_market_tennis_and_combat():
    """Tennis (atp, wta) and combat (ufc) pass."""
    assert is_sport_market("aec-atp-romsaf-pabrui-2026-03-28") is True
    assert is_sport_market("aec-wta-someplayer-otherplayer-2026-05-13") is True
    assert is_sport_market("aec-ufc-israde-joepyf-2026-03-28") is True


def test_is_sport_market_soccer():
    """Soccer leagues (mls, epl, etc.) pass."""
    assert is_sport_market("atc-mls-atl-hou-2026-05-12") is True


def test_is_sport_market_observed_in_trade_tape():
    """Sports observed in real Polymarket data: wnba, cs2, ipl."""
    assert is_sport_market("aec-wnba-lvs-sea-2026-05-15") is True
    assert is_sport_market("aec-cs2-someteam-otherteam-2026-05-13") is True
    assert is_sport_market("aec-ipl-rcb-csk-2026-05-14") is True


def test_is_sport_market_rejects_weather():
    """tc-temp-* weather markets must be REJECTED."""
    assert is_sport_market("tc-temp-laxhigh-2026-05-13-gte67lt68f") is False
    assert is_sport_market("tc-temp-miahigh-2026-05-18-gte88lt89f") is False
    assert is_sport_market("tc-temp-nychigh-2026-05-19-gte95lt96f") is False


def test_is_sport_market_rejects_unknown_categories():
    """Slugs with unknown parts[1] are rejected (whitelist by design)."""
    assert is_sport_market("xyz-cryptoprice-btc-2026-05-13") is False
    assert is_sport_market("foo-bar-baz-2026-05-13") is False


def test_is_sport_market_rejects_malformed_slugs():
    """Empty or short slugs are rejected."""
    assert is_sport_market("") is False
    assert is_sport_market("short") is False
    assert is_sport_market("tsc") is False


def test_is_sport_market_handles_team_prefix_lal():
    """atc-lal-* (Lakers championship futures) was already in _SPORTS."""
    assert is_sport_market("atc-lal-champ-2026-06-01") is True


# --- apply_prefilters ---

def _candidate(spread=5, midpoint=50, yes_depth=200, no_depth=200,
               symmetry=1.0, net_spread=5.25, best_yes_depth=50,
               best_no_depth=50):
    return {
        "slug": "test-market",
        "spread": spread,
        "midpoint": midpoint,
        "yes_depth": yes_depth,
        "no_depth": no_depth,
        "symmetry": symmetry,
        "net_spread": net_spread,
        "best_yes_depth": best_yes_depth,
        "best_no_depth": best_no_depth,
    }


def test_prefilter_passes_good_candidate():
    c = _candidate()
    result = apply_prefilters(c)
    assert result is True


def test_prefilter_passes_1c_spread():
    """1c spread is profitable on Polymarket (maker rebates)."""
    c = _candidate(spread=1, net_spread=1.25)
    assert apply_prefilters(c) is True


def test_prefilter_fails_zero_spread():
    c = _candidate(spread=0)
    assert apply_prefilters(c) is False


def test_prefilter_fails_wide_spread():
    c = _candidate(spread=12)
    assert apply_prefilters(c) is False


def test_prefilter_fails_extreme_midpoint_low():
    c = _candidate(midpoint=15)
    assert apply_prefilters(c) is False


def test_prefilter_fails_extreme_midpoint_high():
    c = _candidate(midpoint=85)
    assert apply_prefilters(c) is False


# --- Tightened midpoint filter: 45-55c (Fix 3 from asymmetry diagnosis) ---

def test_prefilter_fails_just_below_45():
    """Per fill_asymmetry_diagnosis.md Fix 3: extreme-mid markets (<45)
    yielded 14 yes_bid fills vs 0 no_bid fills — disproportionately
    asymmetric tail. Reject anything below 45c."""
    c = _candidate(midpoint=44)
    assert apply_prefilters(c) is False


def test_prefilter_fails_just_above_55():
    c = _candidate(midpoint=56)
    assert apply_prefilters(c) is False


def test_prefilter_passes_lower_boundary_45():
    c = _candidate(midpoint=45)
    assert apply_prefilters(c) is True


def test_prefilter_passes_upper_boundary_55():
    c = _candidate(midpoint=55)
    assert apply_prefilters(c) is True


def test_prefilter_fails_no_depth():
    c = _candidate(best_yes_depth=0)
    assert apply_prefilters(c) is False


def test_prefilter_fails_asymmetric():
    c = _candidate(symmetry=0.1)
    assert apply_prefilters(c) is False


def test_prefilter_fails_negative_net_spread():
    c = _candidate(net_spread=-1)
    assert apply_prefilters(c) is False


# --- avg_rank ---

def test_avg_rank_ascending():
    """Ascending: lowest value gets rank 1."""
    ranks = avg_rank([30, 10, 20], ascending=True)
    assert ranks[0] == 3.0  # 30 = rank 3
    assert ranks[1] == 1.0  # 10 = rank 1
    assert ranks[2] == 2.0  # 20 = rank 2


def test_avg_rank_descending():
    """Descending: highest value gets rank 1."""
    ranks = avg_rank([30, 10, 20], ascending=False)
    assert ranks[0] == 1.0  # 30 = rank 1
    assert ranks[1] == 3.0  # 10 = rank 3
    assert ranks[2] == 2.0  # 20 = rank 2


def test_avg_rank_ties():
    """Tied values get average rank."""
    ranks = avg_rank([10, 10, 20], ascending=True)
    assert ranks[0] == 1.5  # tied for rank 1-2 → avg 1.5
    assert ranks[1] == 1.5
    assert ranks[2] == 3.0


# --- rank_candidates ---

def test_rank_basic():
    """Two-metric ranking: net_spread (desc) + binding_queue (asc)."""
    candidates = [
        {"passes": True, "net_spread": 5, "binding_queue": 100,
         "slug": "a"},
        {"passes": True, "net_spread": 3, "binding_queue": 50,
         "slug": "b"},
        {"passes": True, "net_spread": 8, "binding_queue": 200,
         "slug": "c"},
    ]
    ranked = rank_candidates(candidates)
    passing = [c for c in ranked if c["passes"]]
    # Each has a composite_rank
    assert all("composite_rank" in c for c in passing)
    # Sorted by composite (lowest first)
    composites = [c["composite_rank"] for c in passing]
    assert composites == sorted(composites)


def test_rank_failing_excluded():
    """Failing candidates go to the end, unranked."""
    candidates = [
        {"passes": True, "net_spread": 5, "binding_queue": 100,
         "slug": "a"},
        {"passes": False, "slug": "b"},
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0]["slug"] == "a"
    assert ranked[1]["slug"] == "b"
    assert "composite_rank" not in ranked[1]


def test_rank_empty():
    assert rank_candidates([]) == []


# --- hours_to_game filter ---

from scripts.poly_daily_scan import filter_by_hours_to_game


def test_filter_excludes_distant_game():
    """Game 48h away → passes set to False."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {"slug": "far", "passes": True,
         "game_start_time": "2026-03-31T12:00:00Z"},
    ]
    result = filter_by_hours_to_game(candidates, max_hours=18, now=now)
    assert result[0]["passes"] is False
    assert "48h" in result[0].get("skip_reason", "")


def test_filter_includes_today_game():
    """Game 5h away → included."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {"slug": "soon", "passes": True,
         "game_start_time": "2026-03-29T17:00:00Z"},
    ]
    result = filter_by_hours_to_game(candidates, max_hours=18, now=now)
    assert len(result) == 1


def test_filter_keeps_no_game_start():
    """Market without game_start_time → kept (can't filter)."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {"slug": "unknown", "passes": True, "game_start_time": ""},
    ]
    result = filter_by_hours_to_game(candidates, max_hours=18, now=now)
    assert len(result) == 1


def test_filter_only_affects_passing():
    """Non-passing candidates left untouched."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {"slug": "far-fail", "passes": False,
         "game_start_time": "2026-04-05T12:00:00Z"},
    ]
    result = filter_by_hours_to_game(candidates, max_hours=18, now=now)
    assert len(result) == 1  # kept because passes=False (not filtered)


def test_filter_rejects_game_too_close():
    """Game 1h away → rejected (spread already compressed)."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {"slug": "imminent", "passes": True,
         "game_start_time": "2026-03-29T13:00:00Z"},
    ]
    result = filter_by_hours_to_game(candidates, max_hours=18,
                                     min_hours=3, now=now)
    assert result[0]["passes"] is False
    assert "too close" in result[0].get("skip_reason", "").lower()


def test_filter_accepts_game_at_8h():
    """Game 8h away → accepted (within 3-18h window)."""
    now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {"slug": "tonight", "passes": True,
         "game_start_time": "2026-03-29T20:00:00Z"},
    ]
    result = filter_by_hours_to_game(candidates, max_hours=18,
                                     min_hours=3, now=now)
    assert result[0]["passes"] is True


# --- volume-based taker velocity filter ---
# Replaced the 2-poll BBO delta approach (too slow for 50+ markets)
# with shares_traded from BBO metadata — already fetched, zero extra API calls.

from scripts.poly_daily_scan import filter_by_taker_velocity, MIN_SHARES_TRADED


def test_velocity_rejects_dead_market():
    """Market with 0 shares_traded → rejected (dead water)."""
    candidates = [
        {"slug": "dead-water", "passes": True, "shares_traded": 0},
    ]
    result = filter_by_taker_velocity(candidates, min_shares=MIN_SHARES_TRADED)
    assert result[0]["passes"] is False
    assert "low volume" in result[0].get("skip_reason", "").lower()


def test_velocity_accepts_active_market():
    """Market with high shares_traded → accepted."""
    candidates = [
        {"slug": "active", "passes": True, "shares_traded": 50000},
    ]
    result = filter_by_taker_velocity(candidates, min_shares=MIN_SHARES_TRADED)
    assert result[0]["passes"] is True


def test_velocity_skips_non_passing():
    """Non-passing candidates untouched."""
    candidates = [
        {"slug": "already-failed", "passes": False, "shares_traded": 0},
    ]
    result = filter_by_taker_velocity(candidates, min_shares=MIN_SHARES_TRADED)
    assert result[0]["passes"] is False  # unchanged, no skip_reason added


def test_velocity_at_threshold():
    """Exactly at threshold → accepted."""
    candidates = [
        {"slug": "borderline", "passes": True,
         "shares_traded": MIN_SHARES_TRADED},
    ]
    result = filter_by_taker_velocity(candidates, min_shares=MIN_SHARES_TRADED)
    assert result[0]["passes"] is True


def test_velocity_just_below_threshold():
    """One below threshold → rejected."""
    candidates = [
        {"slug": "almost", "passes": True,
         "shares_traded": MIN_SHARES_TRADED - 1},
    ]
    result = filter_by_taker_velocity(candidates, min_shares=MIN_SHARES_TRADED)
    assert result[0]["passes"] is False


def test_min_shares_traded_is_reasonable():
    """MIN_SHARES_TRADED should be conservative (100-10000 range)."""
    assert 100 <= MIN_SHARES_TRADED <= 10000
