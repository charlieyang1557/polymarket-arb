# tests/test_poly_schedule.py
"""Tests for Polymarket slug-to-schedule matching and game time extraction."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.poly_daily_scan import (
    parse_slug,
    match_slug_to_schedule,
    extract_game_start_from_market,
)


# --- parse_slug ---

def test_parse_slug_totals():
    """tsc-nba-sac-atl-2026-03-28-238pt5 → sport=nba, teams=[sac,atl], date=2026-03-28."""
    result = parse_slug("tsc-nba-sac-atl-2026-03-28-238pt5")
    assert result["sport"] == "nba"
    assert result["team1"] == "sac"
    assert result["team2"] == "atl"
    assert result["date"] == "2026-03-28"


def test_parse_slug_spread():
    """asc-nba-sac-atl-2026-03-28-pos-15pt5 → same teams."""
    result = parse_slug("asc-nba-sac-atl-2026-03-28-pos-15pt5")
    assert result["sport"] == "nba"
    assert result["team1"] == "sac"
    assert result["team2"] == "atl"
    assert result["date"] == "2026-03-28"


def test_parse_slug_moneyline():
    """aec-mlb-pit-nym-2026-03-28 → sport=mlb, teams=[pit,nym]."""
    result = parse_slug("aec-mlb-pit-nym-2026-03-28")
    assert result["sport"] == "mlb"
    assert result["team1"] == "pit"
    assert result["team2"] == "nym"
    assert result["date"] == "2026-03-28"


def test_parse_slug_nhl():
    """tsc-nhl-van-cgy-2026-03-28-6pt5 → sport=nhl, teams=[van,cgy]."""
    result = parse_slug("tsc-nhl-van-cgy-2026-03-28-6pt5")
    assert result["sport"] == "nhl"
    assert result["team1"] == "van"
    assert result["team2"] == "cgy"
    assert result["date"] == "2026-03-28"


def test_parse_slug_cbb():
    """aec-cbb-bayl-minnst-2026-04-01 → sport=cbb, teams=[bayl,minnst]."""
    result = parse_slug("aec-cbb-bayl-minnst-2026-04-01")
    assert result["sport"] == "cbb"
    assert result["team1"] == "bayl"
    assert result["team2"] == "minnst"
    assert result["date"] == "2026-04-01"


def test_parse_slug_tennis():
    """aec-atp-romsaf-pabrui-2026-03-28 → sport=atp."""
    result = parse_slug("aec-atp-romsaf-pabrui-2026-03-28")
    assert result["sport"] == "atp"
    assert result["team1"] == "romsaf"
    assert result["team2"] == "pabrui"
    assert result["date"] == "2026-03-28"


def test_parse_slug_ufc():
    """aec-ufc-israde-joepyf-2026-03-28 → sport=ufc."""
    result = parse_slug("aec-ufc-israde-joepyf-2026-03-28")
    assert result["sport"] == "ufc"
    assert result["team1"] == "israde"
    assert result["team2"] == "joepyf"


def test_parse_slug_futures():
    """tec-nba-champ-2026-07-01-okc → no clear team2, date present."""
    result = parse_slug("tec-nba-champ-2026-07-01-okc")
    assert result["sport"] == "nba"
    assert result["date"] == "2026-07-01"


def test_parse_slug_invalid():
    """Unparseable slug returns None fields."""
    result = parse_slug("something-weird")
    assert result["sport"] is None
    assert result["date"] is None


# --- match_slug_to_schedule ---

SAMPLE_GAMES = [
    {"sport": "NBA", "away_team": "SAC", "home_team": "ATL",
     "start_time_utc": "2026-03-28T23:30:00Z",
     "away_full": "Sacramento Kings", "home_full": "Atlanta Hawks"},
    {"sport": "NHL", "away_team": "VAN", "home_team": "CGY",
     "start_time_utc": "2026-03-29T01:00:00Z",
     "away_full": "Vancouver Canucks", "home_full": "Calgary Flames"},
    {"sport": "MLB", "away_team": "PIT", "home_team": "NYM",
     "start_time_utc": "2026-03-28T23:10:00Z",
     "away_full": "Pittsburgh Pirates", "home_full": "New York Mets"},
]


def test_match_nba_totals():
    result = match_slug_to_schedule(
        "tsc-nba-sac-atl-2026-03-28-238pt5", SAMPLE_GAMES)
    assert result == "2026-03-28T23:30:00Z"


def test_match_nba_spread():
    result = match_slug_to_schedule(
        "asc-nba-sac-atl-2026-03-28-pos-15pt5", SAMPLE_GAMES)
    assert result == "2026-03-28T23:30:00Z"


def test_match_nhl():
    result = match_slug_to_schedule(
        "tsc-nhl-van-cgy-2026-03-28-6pt5", SAMPLE_GAMES)
    assert result == "2026-03-29T01:00:00Z"


def test_match_mlb():
    result = match_slug_to_schedule(
        "aec-mlb-pit-nym-2026-03-28", SAMPLE_GAMES)
    assert result == "2026-03-28T23:10:00Z"


def test_match_no_match():
    result = match_slug_to_schedule(
        "aec-nfl-kc-buf-2026-03-28", SAMPLE_GAMES)
    assert result is None


def test_match_wrong_date():
    """Same teams but different date → no match."""
    result = match_slug_to_schedule(
        "tsc-nba-sac-atl-2026-03-30-238pt5", SAMPLE_GAMES)
    assert result is None


def test_match_case_insensitive():
    """Slug teams lowercase, schedule uppercase → still matches."""
    games = [{"sport": "NBA", "away_team": "sac", "home_team": "atl",
              "start_time_utc": "2026-03-28T23:30:00Z"}]
    result = match_slug_to_schedule(
        "tsc-nba-sac-atl-2026-03-28-238pt5", games)
    assert result == "2026-03-28T23:30:00Z"


def test_match_teams_reversed():
    """Slug has team1-team2 but schedule has team2 as away → still matches."""
    games = [{"sport": "NBA", "away_team": "ATL", "home_team": "SAC",
              "start_time_utc": "2026-03-28T23:30:00Z"}]
    result = match_slug_to_schedule(
        "tsc-nba-sac-atl-2026-03-28-238pt5", games)
    assert result == "2026-03-28T23:30:00Z"


# --- extract_game_start_from_market ---

def test_extract_game_start_present():
    """Market with gameStartTime → extract it."""
    market = {"gameStartTime": "2026-03-28T23:30:00Z"}
    assert extract_game_start_from_market(market) == "2026-03-28T23:30:00Z"


def test_extract_game_start_missing():
    """Market without gameStartTime → None."""
    market = {"gameStartTime": ""}
    assert extract_game_start_from_market(market) is None


def test_extract_game_start_none():
    market = {}
    assert extract_game_start_from_market(market) is None


def test_extract_game_start_nested():
    """Event-level startTime as fallback."""
    market = {"gameStartTime": "", "_event_start_time": "2026-03-28T23:30:00Z"}
    assert extract_game_start_from_market(market) == "2026-03-28T23:30:00Z"
