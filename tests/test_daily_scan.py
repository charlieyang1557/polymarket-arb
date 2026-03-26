"""Tests for daily scanner scoring and ranking."""
import json
import sys
import os
import math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from scripts.kalshi_daily_scan import (
    deep_check, net_spread_cents, rank_candidates,
    ALLOWED_SPORT_PREFIXES, is_allowed_sport,
    load_game_schedule, attach_game_start,
    is_bot_running, zero_market_message,
    write_pending_markets, match_schedule_to_market,
)


# -- Helpers ------------------------------------------------------------------

def _mock_client(trades_per_hour):
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "200"], ["0.46", "300"]],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    now = datetime.now(timezone.utc)
    num_trades = int(trades_per_hour)
    trades = []
    for i in range(num_trades):
        ts = (now - timedelta(seconds=i * (3600 / max(num_trades, 1)))).strftime(
            "%Y-%m-%dT%H:%M:%S.000000Z")
        trades.append({
            "trade_id": f"t{i}",
            "created_time": ts,
            "count_fp": "2",
            "yes_price_dollars": "0.46",
        })
    client.get_trades.return_value = {"trades": trades}
    return client


# -- net_spread_cents ---------------------------------------------------------

def test_net_spread_positive():
    """Spread of 5c at midpoint 50c: maker_fee = 0.0175*50*50/100 = 0.4375c
    per side, round up to 1c each. net_spread = 5 - 2*1 = 3."""
    assert net_spread_cents(5, 50.0) == 3


def test_net_spread_zero_at_thin_spread():
    """Spread of 2c at midpoint 50c: fees eat the entire spread."""
    result = net_spread_cents(2, 50.0)
    assert result <= 0


def test_net_spread_high_midpoint():
    """At midpoint 90c: fee = ceil(0.0175*90*10/100) = ceil(0.1575) = 1c.
    Spread 4 → net = 4 - 2*1 = 2."""
    assert net_spread_cents(4, 90.0) == 2


def test_net_spread_low_midpoint():
    """At midpoint 10c: fee = ceil(0.0175*10*90/100) = ceil(0.1575) = 1c.
    Spread 4 → net = 4 - 2*1 = 2."""
    assert net_spread_cents(4, 10.0) == 2


def test_net_spread_midpoint_50_spread_3():
    """Midpoint 50c: fee per side = ceil(0.0175*50*50/100) = ceil(0.4375) = 1c.
    Spread 3 → net = 3 - 2 = 1."""
    assert net_spread_cents(3, 50.0) == 1


# -- deep_check ---------------------------------------------------------------

def test_deep_check_adds_trades_per_hour():
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "trades_per_hour" in result[0]
    assert result[0]["trades_per_hour"] > 0


def test_deep_check_adds_net_spread():
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "net_spread" in result[0]
    assert result[0]["net_spread"] >= 1


def test_deep_check_adds_binding_queue():
    """binding_queue = max(yes_depth, no_depth)."""
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "binding_queue" in result[0]
    # yes_depth = 200+300=500, no_depth = 200+300=500 → binding = 500
    assert result[0]["binding_queue"] == 500


def test_deep_check_passes_good_market():
    client = _mock_client(100)
    candidates = [{"ticker": "GOOD", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is True


def test_deep_check_fails_huge_l1_queue():
    """Market with L1 best depth >= 20000 should fail."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "25000"], ["0.46", "300"]],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 36)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.46"}
              for i in range(100)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "BIGQ", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False
    assert result[0]["yes_best_depth"] == 25000
    # Also verify max_best_depth is exposed for display
    assert result[0]["max_best_depth"] == 25000


def test_deep_check_exposes_max_best_depth():
    """max_best_depth should be stored on candidate for display."""
    client = _mock_client(100)
    candidates = [{"ticker": "TEST", "spread": 5, "midpoint": 48,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert "max_best_depth" in result[0]
    # L1 is 200 on both sides → max = 200
    assert result[0]["max_best_depth"] == 200


def test_deep_check_fails_wide_net_spread():
    """Net spread > 8 should fail — wide spreads have asymmetric liquidity."""
    client = _mock_client(100)
    # spread=14 at midpoint 50 → net = 14 - 2*1 = 12 > 8
    candidates = [{"ticker": "WIDE", "spread": 14, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["net_spread"] == 12
    assert result[0].get("passes") is False


def test_deep_check_passes_net_spread_at_boundary():
    """Net spread == 8 should still pass (upper bound inclusive)."""
    client = _mock_client(100)
    # spread=10 at midpoint 50 → net = 10 - 2*1 = 8
    candidates = [{"ticker": "BOUNDARY", "spread": 10, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["net_spread"] == 8
    assert result[0].get("passes") is True


def test_deep_check_fails_empty_yes_book():
    """Market with no YES levels should fail."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [],
            "no_dollars": [["0.52", "200"], ["0.53", "300"]],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 36)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.46"}
              for i in range(100)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "NOYES", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["yes_best_depth"] == 0
    assert result[0].get("passes") is False


def test_deep_check_fails_empty_no_book():
    """Market with no NO levels should fail."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.45", "200"], ["0.46", "300"]],
            "no_dollars": [],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 36)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.46"}
              for i in range(100)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "NONO", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["no_best_depth"] == 0
    assert result[0].get("passes") is False


def test_deep_check_fails_low_freq():
    client = _mock_client(5)
    candidates = [{"ticker": "SLOW", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_fails_negative_net_spread():
    """Spread of 1c should fail — fees exceed spread."""
    client = _mock_client(100)
    candidates = [{"ticker": "THIN", "spread": 1, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_passes_net_spread_1():
    """Net spread == 1 should pass (raw spread=3c at mid=50 → net=1c after fees)."""
    client = _mock_client(100)
    # spread=3 at midpoint 50 → fee = ceil(0.0175*50*50/100) = 1c
    # net = 3 - 2*1 = 1
    candidates = [{"ticker": "THIN_OK", "spread": 3, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["net_spread"] == 1
    assert result[0].get("passes") is True


def test_deep_check_fails_net_spread_0():
    """Net spread == 0 should fail."""
    client = _mock_client(100)
    # spread=2 at midpoint 50 → net = 2 - 2*1 = 0
    candidates = [{"ticker": "ZERO_NET", "spread": 2, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["net_spread"] == 0
    assert result[0].get("passes") is False


def test_deep_check_fails_expiring_soon():
    """Market expiring in 30 minutes should fail."""
    client = _mock_client(100)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    candidates = [{"ticker": "EXPIRING", "spread": 5, "midpoint": 48,
                   "volume_24h": 5000,
                   "expected_expiration": soon}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


# -- rank_candidates ----------------------------------------------------------

def test_rank_candidates_ordering():
    """Best market = highest net_spread + lowest queue + highest freq."""
    candidates = [
        {"ticker": "A", "net_spread": 5, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
        {"ticker": "B", "net_spread": 3, "binding_queue": 500,
         "trades_per_hour": 20, "passes": True},
        {"ticker": "C", "net_spread": 1, "binding_queue": 1000,
         "trades_per_hour": 10, "passes": True},
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0]["ticker"] == "A"
    assert ranked[-1]["ticker"] == "C"


def test_rank_candidates_uses_average_ties():
    """Tied markets should get average rank."""
    candidates = [
        {"ticker": "A", "net_spread": 3, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
        {"ticker": "B", "net_spread": 3, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
    ]
    ranked = rank_candidates(candidates)
    # Both should have identical composite scores
    assert ranked[0]["composite_rank"] == ranked[1]["composite_rank"]


def test_rank_candidates_only_ranks_passing():
    """Non-passing candidates should not get composite_rank."""
    candidates = [
        {"ticker": "GOOD", "net_spread": 5, "binding_queue": 100,
         "trades_per_hour": 50, "passes": True},
        {"ticker": "BAD", "net_spread": -1, "binding_queue": 100,
         "trades_per_hour": 50, "passes": False},
    ]
    ranked = rank_candidates(candidates)
    passing = [c for c in ranked if c.get("passes")]
    failing = [c for c in ranked if not c.get("passes")]
    assert len(passing) == 1
    assert passing[0]["ticker"] == "GOOD"
    assert "composite_rank" in passing[0]
    # Failing markets should not have composite_rank
    assert "composite_rank" not in failing[0]


def test_rank_candidates_mixed_strengths():
    """Market good at spread but bad at queue should rank middle."""
    candidates = [
        {"ticker": "SPREAD_KING", "net_spread": 10, "binding_queue": 5000,
         "trades_per_hour": 10, "passes": True},
        {"ticker": "ALL_ROUNDER", "net_spread": 5, "binding_queue": 200,
         "trades_per_hour": 30, "passes": True},
        {"ticker": "FREQ_KING", "net_spread": 2, "binding_queue": 100,
         "trades_per_hour": 100, "passes": True},
    ]
    ranked = rank_candidates(candidates)
    # FREQ_KING dominates 2/3 axes (queue=1, freq=1) → composite 1.67
    # ALL_ROUNDER is middle on all (2,2,2) → composite 2.0
    # SPREAD_KING dominates 1 axis but worst on 2 → composite 2.33
    assert ranked[0]["ticker"] == "FREQ_KING"
    assert ranked[1]["ticker"] == "ALL_ROUNDER"
    assert ranked[2]["ticker"] == "SPREAD_KING"


# -- Sport prefix filter --------------------------------------------------

def test_allowed_sport_nba():
    assert is_allowed_sport("KXNBASPREAD-26MAR19DETWAS-DET25") is True

def test_allowed_sport_ncaa_mens():
    assert is_allowed_sport("KXNCAAMBSPREAD-26MAR19IDHOHOU-HOU22") is True

def test_allowed_sport_ncaa_womens():
    assert is_allowed_sport("KXNCAAWBSPREAD-26MAR19FOO-BAR1") is True

def test_allowed_sport_nhl():
    assert is_allowed_sport("KXNHLSPREAD-26MAR19FOO-BAR1") is True

def test_allowed_sport_mlb():
    assert is_allowed_sport("KXMLBSPREAD-26MAR19FOO-BAR1") is True

def test_allowed_sport_nfl():
    assert is_allowed_sport("KXNFLSPREAD-26SEP19FOO-BAR1") is True

def test_rejected_esport_lol_no_schedule():
    """E-sports without game_start_utc rejected — no deterministic exit."""
    assert is_allowed_sport("KXLOLTOTALMAPS-26MAR19LYGEN-4") is False

def test_rejected_esport_csgo_no_schedule():
    assert is_allowed_sport("KXCSGOTOTALMAPS-26MAR19FOO-BAR") is False

def test_rejected_unknown_prefix():
    assert is_allowed_sport("KXCRICKETSPREAD-26MAR19FOO-BAR") is False

def test_allowed_prefixes_list():
    """Verify the allowed list contains expected sports."""
    assert "KXNBA" in ALLOWED_SPORT_PREFIXES
    assert "KXNCAAMB" in ALLOWED_SPORT_PREFIXES
    assert "KXNCAAWB" in ALLOWED_SPORT_PREFIXES
    assert "KXNHL" in ALLOWED_SPORT_PREFIXES
    assert "KXNFL" in ALLOWED_SPORT_PREFIXES
    assert "KXMLB" in ALLOWED_SPORT_PREFIXES


# -- Conditional e-sports allowance ----------------------------------------

def test_esport_allowed_with_future_game_start():
    """E-sports WITH valid future game_start_utc should pass."""
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    assert is_allowed_sport("KXLOLTOTALMAPS-26MAR19LYGEN-4",
                            game_start_utc=future) is True

def test_esport_blocked_with_past_game_start():
    """E-sports with past game_start_utc should be blocked."""
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert is_allowed_sport("KXLOLTOTALMAPS-26MAR19LYGEN-4",
                            game_start_utc=past) is False

def test_esport_blocked_with_imminent_game_start():
    """E-sports with game_start_utc within 15min should be blocked."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    assert is_allowed_sport("KXLOLTOTALMAPS-26MAR19LYGEN-4",
                            game_start_utc=soon) is False

def test_esport_allowed_at_16min_boundary():
    """E-sports with game_start_utc at exactly 16min should pass."""
    boundary = (datetime.now(timezone.utc) + timedelta(minutes=16)).isoformat()
    assert is_allowed_sport("KXCSGOTOTALMAPS-26MAR19FOO-BAR",
                            game_start_utc=boundary) is True

def test_traditional_sport_still_passes_without_schedule():
    """NBA etc. still pass without game_start_utc (unchanged behavior)."""
    assert is_allowed_sport("KXNBASPREAD-26MAR19DETWAS-DET25") is True

def test_esport_blocked_with_none_game_start():
    """Explicit None game_start_utc should not bypass whitelist."""
    assert is_allowed_sport("KXLOLTOTALMAPS-26MAR19LYGEN-4",
                            game_start_utc=None) is False


# -- Midpoint filter ----------------------------------------------------------

def test_deep_check_fails_high_midpoint():
    """Market with midpoint 67c (alt-line) should fail."""
    client = _mock_client(100)
    candidates = [{"ticker": "ALTLINE", "spread": 5, "midpoint": 67,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_fails_low_midpoint():
    """Market with midpoint 30c (extreme underdog) should fail."""
    client = _mock_client(100)
    candidates = [{"ticker": "UNDERDOG", "spread": 5, "midpoint": 30,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_passes_midpoint_at_boundaries():
    """Midpoints at 35c and 65c should pass (inclusive)."""
    client = _mock_client(100)
    for mid in (35, 65):
        candidates = [{"ticker": f"BOUNDARY{mid}", "spread": 5, "midpoint": mid,
                       "volume_24h": 5000,
                       "expected_expiration": "2099-12-31T23:59:59Z"}]
        result = deep_check(client, candidates, max_check=1)
        assert result[0].get("passes") is True, f"midpoint={mid} should pass"


def test_deep_check_passes_midpoint_50():
    """Midpoint 50c (main line) should pass."""
    client = _mock_client(100)
    candidates = [{"ticker": "MAINLINE", "spread": 5, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is True


# -- Game schedule integration ------------------------------------------------

def test_schedule_lookup_matches_ticker(tmp_path):
    """Schedule file maps ticker to game start time."""
    schedule_file = tmp_path / "game_schedule.json"
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": fresh,
        "games": [{
            "sport": "NCAA",
            "away_team": "PV",
            "home_team": "FLA",
            "start_time_utc": "2026-03-21T01:25:00Z",
            "kalshi_markets": ["KXNCAAMBSPREAD-26MAR20PVFLA-FLA47"]
        }]
    }))
    schedule, _ = load_game_schedule(str(schedule_file))
    assert schedule["KXNCAAMBSPREAD-26MAR20PVFLA-FLA47"] == "2026-03-21T01:25:00Z"


def test_schedule_lookup_missing_file_graceful():
    """Missing schedule file returns empty dict, no crash."""
    schedule, games = load_game_schedule("/nonexistent/path/game_schedule.json")
    assert schedule == {}
    assert games == []


def test_schedule_lookup_ticker_not_in_schedule(tmp_path):
    """Ticker not in schedule → no game_start_utc attached."""
    schedule_file = tmp_path / "game_schedule.json"
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": fresh,
        "games": [{
            "sport": "NCAA",
            "start_time_utc": "2026-03-21T01:25:00Z",
            "kalshi_markets": ["KXNCAAMBSPREAD-OTHER"]
        }]
    }))
    schedule, _ = load_game_schedule(str(schedule_file))
    candidates = [{"ticker": "KXNCAAMBSPREAD-NOTFOUND", "passes": True}]
    attach_game_start(candidates, schedule)
    assert "game_start_utc" not in candidates[0]


def test_schedule_lookup_null_kalshi_markets(tmp_path):
    """kalshi_markets: null should not crash — treat as empty list."""
    schedule_file = tmp_path / "game_schedule.json"
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": fresh,
        "games": [{
            "sport": "NCAA",
            "start_time_utc": "2026-03-21T01:25:00Z",
            "kalshi_markets": None
        }]
    }))
    schedule, _ = load_game_schedule(str(schedule_file))
    assert schedule == {}


# -- Zero-market Discord notification ----------------------------------------

def test_zero_market_message_format():
    """Message contains total count, checked time, and next scan time."""
    now = datetime(2026, 3, 22, 12, 57, 0, tzinfo=timezone.utc)
    msg = zero_market_message(total=12, now=now)
    assert "0/12" in msg
    assert "12:57" in msg
    assert "Next scan:" in msg


def test_zero_market_message_morning_next_is_afternoon():
    """Morning scan (12:57 UTC) → next scan should be afternoon (20:00 UTC)."""
    now = datetime(2026, 3, 22, 12, 57, 0, tzinfo=timezone.utc)
    msg = zero_market_message(total=8, now=now)
    assert "20:00" in msg


def test_zero_market_message_afternoon_next_is_morning():
    """Afternoon scan (20:00 UTC) → next scan should be tomorrow morning (12:57 UTC)."""
    now = datetime(2026, 3, 22, 20, 0, 0, tzinfo=timezone.utc)
    msg = zero_market_message(total=5, now=now)
    assert "12:57" in msg


def test_attach_game_start_adds_field(tmp_path):
    """attach_game_start sets game_start_utc on matching candidates."""
    schedule = {"KXTICKER1": "2026-03-21T01:00:00Z"}
    candidates = [
        {"ticker": "KXTICKER1", "passes": True},
        {"ticker": "KXTICKER2", "passes": True},
    ]
    attach_game_start(candidates, schedule)
    assert candidates[0]["game_start_utc"] == "2026-03-21T01:00:00Z"
    assert "game_start_utc" not in candidates[1]


# -- is_bot_running -----------------------------------------------------------

def test_is_bot_running_returns_true_when_running():
    """pgrep returns 0 → bot is running, returns (True, pid)."""
    mock_result = MagicMock(returncode=0, stdout=b"12345\n")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        running, pid = is_bot_running()
        assert running is True
        assert pid == "12345"
        mock_run.assert_called_once()


def test_is_bot_running_returns_false_when_not_running():
    """pgrep returns 1 → bot not running."""
    mock_result = MagicMock(returncode=1, stdout=b"")
    with patch("subprocess.run", return_value=mock_result):
        running, pid = is_bot_running()
        assert running is False
        assert pid is None


def test_is_bot_running_handles_multiple_pids():
    """Multiple PIDs → returns first (oldest)."""
    mock_result = MagicMock(returncode=0, stdout=b"12345\n67890\n")
    with patch("subprocess.run", return_value=mock_result):
        running, pid = is_bot_running()
        assert running is True
        assert pid == "12345"


# -- Pending markets (hot-add) -----------------------------------------------

def test_write_pending_markets_atomic(tmp_path):
    """write_pending_markets writes JSON atomically via .tmp rename."""
    targets = [
        {"ticker": "KXNBASPREAD-T1", "title": "NBA Game 1",
         "game_start_utc": "2026-03-26T23:00:00Z"},
        {"ticker": "KXNHLSPREAD-T2", "title": "NHL Game 2"},
    ]
    out_path = str(tmp_path / "pending_markets.json")
    n = write_pending_markets(targets, out_path)
    assert n == 2
    assert (tmp_path / "pending_markets.json").exists()
    data = json.loads((tmp_path / "pending_markets.json").read_text())
    assert len(data) == 2
    assert data[0]["ticker"] == "KXNBASPREAD-T1"
    # .tmp should not linger
    assert not (tmp_path / "pending_markets.json.tmp").exists()


def test_write_pending_markets_excludes_active(tmp_path):
    """Tickers already in running session are excluded from pending."""
    targets = [
        {"ticker": "KXNBASPREAD-T1", "title": "Game 1"},
        {"ticker": "KXNBASPREAD-T2", "title": "Game 2"},
    ]
    active_tickers = {"KXNBASPREAD-T1"}
    out_path = str(tmp_path / "pending.json")
    n = write_pending_markets(targets, out_path, active_tickers=active_tickers)
    assert n == 1
    data = json.loads((tmp_path / "pending.json").read_text())
    assert len(data) == 1
    assert data[0]["ticker"] == "KXNBASPREAD-T2"


def test_write_pending_markets_empty_returns_zero(tmp_path):
    """All tickers already active → writes nothing, returns 0."""
    targets = [{"ticker": "KXNBA-T1"}]
    out_path = str(tmp_path / "pending.json")
    n = write_pending_markets(targets, out_path, active_tickers={"KXNBA-T1"})
    assert n == 0
    assert not (tmp_path / "pending.json").exists()


# -- Schedule staleness -------------------------------------------------------

def test_schedule_staleness_revokes_esports(tmp_path):
    """Schedule >6h old → returns empty dict."""
    schedule_file = tmp_path / "game_schedule.json"
    old_time = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": old_time,
        "games": [{
            "sport": "LOL",
            "start_time_utc": "2026-03-26T23:00:00Z",
            "kalshi_markets": ["KXLOLTOTALMAPS-T1"]
        }]
    }))
    schedule, _ = load_game_schedule(str(schedule_file))
    assert schedule == {}


def test_schedule_fresh_allows_esports(tmp_path):
    """Schedule <6h old → e-sports tickers get game_start_utc."""
    schedule_file = tmp_path / "game_schedule.json"
    fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": fresh_time,
        "games": [{
            "sport": "LOL",
            "start_time_utc": "2026-03-26T23:00:00Z",
            "kalshi_markets": ["KXLOLTOTALMAPS-T1"]
        }]
    }))
    schedule, _ = load_game_schedule(str(schedule_file))
    assert "KXLOLTOTALMAPS-T1" in schedule


def test_schedule_no_updated_at_treated_as_stale(tmp_path):
    """Missing updated_at → treat as stale, return empty."""
    schedule_file = tmp_path / "game_schedule.json"
    schedule_file.write_text(json.dumps({
        "games": [{
            "sport": "NBA",
            "start_time_utc": "2026-03-26T23:00:00Z",
            "kalshi_markets": ["KXNBASPREAD-T1"]
        }]
    }))
    schedule, _ = load_game_schedule(str(schedule_file))
    assert schedule == {}


# -- Schedule-to-market matching -----------------------------------------------

def _game(away="WPG", home="NYR", away_full="Winnipeg Jets",
          home_full="New York Rangers",
          start="2026-03-22T23:00:00Z", sport="NHL"):
    """Helper to build a schedule game entry."""
    return {
        "sport": sport,
        "away_team": away, "home_team": home,
        "away_full": away_full, "home_full": home_full,
        "start_time_utc": start, "kalshi_markets": [],
    }


def test_match_contiguous_ticker_abbreviation():
    """PRIORITY 1: 'WPGNYR' found contiguously in ticker."""
    games = [_game(away="WPG", home="NYR")]
    result = match_schedule_to_market(
        games, "KXNHLTOTAL-26MAR22WPGNYR-5", "2026-03-23T04:00:00Z")
    assert result == "2026-03-22T23:00:00Z"


def test_match_contiguous_reversed_order():
    """PRIORITY 1: home+away order also matches."""
    games = [_game(away="WPG", home="NYR")]
    result = match_schedule_to_market(
        games, "KXNHLTOTAL-26MAR22NYRWPG-5", "2026-03-23T04:00:00Z")
    assert result == "2026-03-22T23:00:00Z"


def test_reject_separate_abbreviation_match():
    """'IN'+'LA' must NOT match 'LALMIN' — abbreviations must be contiguous."""
    games = [_game(away="IN", home="LA", away_full="Indiana Pacers",
                   home_full="Los Angeles Lakers", sport="NBA")]
    result = match_schedule_to_market(
        games, "KXNBASPREAD-26MAR22LALMIN-LAL5", "2026-03-23T04:00:00Z")
    # "INLA" or "LAIN" is NOT in "LALMIN", so no ticker match
    # Title fallback needs both teams — not testing that here
    assert result is None


def test_match_title_fallback_both_teams():
    """PRIORITY 2: Token intersection — both team names in event title."""
    games = [_game(away="NYK", home="CHA",
                   away_full="New York Knicks", home_full="Charlotte Hornets",
                   sport="NBA")]
    result = match_schedule_to_market(
        games, "KXNBASPREAD-26MAR22NYKCHR-NYK5", "2026-03-23T04:00:00Z",
        event_title="Knicks at Hornets")
    assert result is not None


def test_reject_one_team_only_title():
    """Title match requires BOTH teams, not just one."""
    games = [_game(away="NYK", home="CHA",
                   away_full="New York Knicks", home_full="Charlotte Hornets",
                   sport="NBA")]
    result = match_schedule_to_market(
        games, "KXNBASPREAD-26MAR22XXXYYY-X1", "2026-03-23T04:00:00Z",
        event_title="Knicks vs Thunder")
    assert result is None


def test_match_date_boundary_pt_evening():
    """PT evening game (MAR22 10PM PT) = UTC MAR23 5AM. Ticker says MAR22."""
    games = [_game(start="2026-03-23T05:00:00Z")]  # game at 10PM PT Mar22
    # Event expires MAR23 in UTC (next day) — within 24h of game start
    result = match_schedule_to_market(
        games, "KXNHLTOTAL-26MAR22WPGNYR-5", "2026-03-23T10:00:00Z")
    assert result == "2026-03-23T05:00:00Z"


def test_reject_date_too_far():
    """Game 48h away should not match."""
    games = [_game(start="2026-03-24T23:00:00Z")]
    result = match_schedule_to_market(
        games, "KXNHLTOTAL-26MAR22WPGNYR-5", "2026-03-22T23:00:00Z")
    assert result is None


def test_match_ncaa_ticker():
    """NCAA tickers: 'ILST'+'DAY' in KXNCAAMBSPREAD ticker."""
    games = [_game(away="ILST", home="DAY",
                   away_full="Illinois State Redbirds",
                   home_full="Dayton Flyers",
                   sport="NCAA", start="2026-03-22T23:00:00Z")]
    result = match_schedule_to_market(
        games, "KXNCAAMBSPREAD-26MAR22ILSTDAY-DAY5", "2026-03-23T04:00:00Z")
    assert result == "2026-03-22T23:00:00Z"


def test_match_case_insensitive():
    """Ticker matching is case-insensitive."""
    games = [_game(away="wpg", home="nyr")]
    result = match_schedule_to_market(
        games, "KXNHLTOTAL-26MAR22WPGNYR-5", "2026-03-23T04:00:00Z")
    assert result == "2026-03-22T23:00:00Z"


# -- Schedule-aware expiry filter (FIX 2) ------------------------------------

def test_deep_check_passes_low_exp_with_game_start():
    """Market expiring in 0.8h but game starts in 3h → should PASS."""
    client = _mock_client(100)
    now = datetime.now(timezone.utc)
    # Expires in 0.8h (would fail old filter)
    exp = (now + timedelta(minutes=48)).isoformat()
    # Game starts in 3h (plenty of pre-game time)
    game_start = (now + timedelta(hours=3)).isoformat()
    candidates = [{"ticker": "SCHED", "spread": 5, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": exp,
                   "game_start_utc": game_start}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is True


def test_deep_check_fails_game_starting_soon():
    """Game starts in 30 min → should FAIL even if exp is far away."""
    client = _mock_client(100)
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(hours=5)).isoformat()
    game_start = (now + timedelta(minutes=30)).isoformat()
    candidates = [{"ticker": "SOON", "spread": 5, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": exp,
                   "game_start_utc": game_start}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


def test_deep_check_no_schedule_falls_back_to_exp():
    """No game_start_utc → use exp>1h as before."""
    client = _mock_client(100)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    candidates = [{"ticker": "NOSTART", "spread": 5, "midpoint": 50,
                   "volume_24h": 5000,
                   "expected_expiration": soon}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False


# -- Relaxed L1Q for high-volume markets (FIX 3) ----------------------------

def test_deep_check_passes_high_l1q_high_freq():
    """L1Q=100k but freq>=50/hr → should PASS (March Madness)."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.48", "100000"], ["0.47", "5000"]],
            "no_dollars": [["0.52", "80000"], ["0.53", "5000"]],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 60)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.48"}
              for i in range(55)]  # 55 trades in ~55 min ≈ 55/hr
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "NCAAMM", "spread": 4, "midpoint": 50,
                   "volume_24h": 100000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0]["max_best_depth"] == 100000
    assert result[0]["trades_per_hour"] >= 50
    assert result[0].get("passes") is True


def test_deep_check_fails_high_l1q_low_freq():
    """L1Q=100k but freq=5/hr → should FAIL (dead political market)."""
    client = MagicMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.48", "100000"]],
            "no_dollars": [["0.52", "80000"]],
        }
    }
    now = datetime.now(timezone.utc)
    trades = [{"trade_id": f"t{i}",
               "created_time": (now - timedelta(seconds=i * 600)).strftime(
                   "%Y-%m-%dT%H:%M:%S.000000Z"),
               "count_fp": "2", "yes_price_dollars": "0.48"}
              for i in range(5)]
    client.get_trades.return_value = {"trades": trades}

    candidates = [{"ticker": "DEAD", "spread": 4, "midpoint": 50,
                   "volume_24h": 1000,
                   "expected_expiration": "2099-12-31T23:59:59Z"}]
    result = deep_check(client, candidates, max_check=1)
    assert result[0].get("passes") is False
