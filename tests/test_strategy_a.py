"""Tests for Strategy A: de-vigging, event matching, DB idempotency."""
import os
import sys
import sqlite3
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# De-vigging
# ---------------------------------------------------------------------------

class TestDevig:
    """Multiplicative de-vig: raw implied probs → fair probs summing to 1."""

    def test_basic_devig(self):
        """Pinnacle-style odds: 1.85 / 2.05 → ~52.5% / ~47.5%."""
        from src.strategy_a.devig import devig_decimal
        home, away, vig = devig_decimal(1.85, 2.05)
        assert abs(home + away - 1.0) < 0.001  # sums to 1
        assert 0.52 < home < 0.54  # ~52.5%
        assert 0.46 < away < 0.48  # ~47.5%
        assert 0.02 < vig < 0.04   # ~2.9% overround

    def test_even_odds(self):
        """Even odds 1.91/1.91 → 50/50 after de-vig."""
        from src.strategy_a.devig import devig_decimal
        home, away, vig = devig_decimal(1.91, 1.91)
        assert abs(home - 0.5) < 0.01
        assert abs(away - 0.5) < 0.01

    def test_heavy_favorite(self):
        """Heavy favorite: 1.15 / 6.00 → ~85% / ~15%."""
        from src.strategy_a.devig import devig_decimal
        home, away, vig = devig_decimal(1.15, 6.00)
        assert home > 0.80
        assert away < 0.20
        assert abs(home + away - 1.0) < 0.001

    def test_no_vig(self):
        """Perfectly fair odds (vig=0): 2.0/2.0 → 50/50."""
        from src.strategy_a.devig import devig_decimal
        home, away, vig = devig_decimal(2.0, 2.0)
        assert abs(home - 0.5) < 0.001
        assert abs(away - 0.5) < 0.001
        assert abs(vig) < 0.001

    def test_american_to_decimal(self):
        """Convert American odds to decimal."""
        from src.strategy_a.devig import american_to_decimal
        assert abs(american_to_decimal(-150) - 1.6667) < 0.001
        assert abs(american_to_decimal(+200) - 3.0) < 0.001
        assert abs(american_to_decimal(-100) - 2.0) < 0.001
        assert abs(american_to_decimal(+100) - 2.0) < 0.001

    def test_invalid_odds_raises(self):
        """Odds <= 1.0 are invalid."""
        from src.strategy_a.devig import devig_decimal
        with pytest.raises(ValueError):
            devig_decimal(0.5, 2.0)
        with pytest.raises(ValueError):
            devig_decimal(2.0, 1.0)


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------

class TestEventMatching:
    """Match Odds-API events to Polymarket slugs via team name extraction."""

    def test_extract_teams_from_slug(self):
        """Parse Polymarket slug into team abbreviations."""
        from src.strategy_a.matching import extract_teams_from_slug
        teams = extract_teams_from_slug("aec-nba-bos-mia-2026-04-01")
        assert teams == ("bos", "mia")

    def test_extract_teams_spread_slug(self):
        """Spread slug has extra suffix."""
        from src.strategy_a.matching import extract_teams_from_slug
        teams = extract_teams_from_slug("asc-nba-bos-mia-2026-04-01-pos-5pt5")
        assert teams == ("bos", "mia")

    def test_normalize_team_name(self):
        """Normalize full team names to Polymarket slug abbreviation."""
        from src.strategy_a.matching import normalize_team
        assert normalize_team("Boston Celtics") == "bos"
        assert normalize_team("Miami Heat") == "mia"
        assert normalize_team("Los Angeles Lakers") == "lal"
        assert normalize_team("LA Lakers") == "lal"
        # Polymarket-specific abbreviations (verified from live slugs)
        assert normalize_team("New York Knicks") == "ny"   # not nyk
        assert normalize_team("Golden State Warriors") == "gs"  # not gsw
        assert normalize_team("Phoenix Suns") == "pho"  # not phx
        assert normalize_team("Los Angeles Kings") == "la"  # not lak
        assert normalize_team("San Jose Sharks") == "sj"  # not sjk
        assert normalize_team("Montréal Canadiens") == "mtl"  # accent
        assert normalize_team("Vegas Golden Knights") == "veg"  # not vgk
        assert normalize_team("Arizona Diamondbacks") == "az"  # not ari
        assert normalize_team("Washington Nationals") == "wsh"  # not was
        assert normalize_team("Chicago White Sox") == "cws"  # not chw

    def test_match_event_to_slug(self):
        """Full matching: odds-api event → polymarket slug."""
        from src.strategy_a.matching import match_event_to_slugs
        slugs = [
            "aec-nba-bos-mia-2026-04-01",
            "aec-nba-lal-gsw-2026-04-01",
            "aec-nhl-nyr-bos-2026-04-01",
        ]
        # Odds-API provides home/away team names
        result = match_event_to_slugs(
            home_team="Boston Celtics",
            away_team="Miami Heat",
            sport="basketball_nba",
            commence_time="2026-04-01T23:30:00Z",
            slugs=slugs,
        )
        assert result == "aec-nba-bos-mia-2026-04-01"

    def test_no_match_returns_none(self):
        """Unmatched event returns None."""
        from src.strategy_a.matching import match_event_to_slugs
        result = match_event_to_slugs(
            home_team="Dallas Mavericks",
            away_team="Phoenix Suns",
            sport="basketball_nba",
            commence_time="2026-04-01T23:30:00Z",
            slugs=["aec-nba-bos-mia-2026-04-01"],
        )
        assert result is None


# ---------------------------------------------------------------------------
# SQLite idempotency
# ---------------------------------------------------------------------------

class TestOddsDB:
    """Inserting the same snapshot twice → one row."""

    def test_insert_and_query(self):
        from src.strategy_a.odds_db import OddsDB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = OddsDB(db_path)
            db.insert_snapshot({
                "timestamp": "2026-04-01T20:00:00Z",
                "slug": "aec-nba-bos-mia-2026-04-01",
                "sport": "basketball_nba",
                "event": "Boston Celtics vs Miami Heat",
                "game_start": "2026-04-01T23:30:00Z",
                "hours_to_game": 3.5,
                "pinnacle_home_prob": 0.62,
                "pinnacle_away_prob": 0.38,
                "pinnacle_raw_home": 1.58,
                "pinnacle_raw_away": 2.45,
                "pinnacle_vig": 0.028,
                "poly_yes_price": 0.60,
                "poly_no_price": 0.40,
                "poly_spread": 2,
                "delta_home": 0.02,
                "delta_away": -0.02,
                "market_type": "moneyline",
            })
            rows = db.get_all()
            assert len(rows) == 1
            assert rows[0]["slug"] == "aec-nba-bos-mia-2026-04-01"
            db.close()
        finally:
            os.unlink(db_path)

    def test_idempotent_insert(self):
        """Same timestamp+slug inserted twice → still one row."""
        from src.strategy_a.odds_db import OddsDB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = OddsDB(db_path)
            snapshot = {
                "timestamp": "2026-04-01T20:00:00Z",
                "slug": "aec-nba-bos-mia-2026-04-01",
                "sport": "basketball_nba",
                "event": "BOS vs MIA",
                "game_start": "2026-04-01T23:30:00Z",
                "hours_to_game": 3.5,
                "pinnacle_home_prob": 0.62,
                "pinnacle_away_prob": 0.38,
                "pinnacle_raw_home": 1.58,
                "pinnacle_raw_away": 2.45,
                "pinnacle_vig": 0.028,
                "poly_yes_price": 0.60,
                "poly_no_price": 0.40,
                "poly_spread": 2,
                "delta_home": 0.02,
                "delta_away": -0.02,
                "market_type": "moneyline",
            }
            db.insert_snapshot(snapshot)
            db.insert_snapshot(snapshot)  # duplicate
            rows = db.get_all()
            assert len(rows) == 1  # idempotent
            db.close()
        finally:
            os.unlink(db_path)

    def test_different_timestamps_both_stored(self):
        """Same slug at different timestamps → two rows."""
        from src.strategy_a.odds_db import OddsDB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = OddsDB(db_path)
            base = {
                "slug": "aec-nba-bos-mia-2026-04-01",
                "sport": "basketball_nba",
                "event": "BOS vs MIA",
                "game_start": "2026-04-01T23:30:00Z",
                "hours_to_game": 3.5,
                "pinnacle_home_prob": 0.62,
                "pinnacle_away_prob": 0.38,
                "pinnacle_raw_home": 1.58,
                "pinnacle_raw_away": 2.45,
                "pinnacle_vig": 0.028,
                "poly_yes_price": 0.60,
                "poly_no_price": 0.40,
                "poly_spread": 2,
                "delta_home": 0.02,
                "delta_away": -0.02,
                "market_type": "moneyline",
            }
            db.insert_snapshot({**base, "timestamp": "2026-04-01T16:00:00Z"})
            db.insert_snapshot({**base, "timestamp": "2026-04-01T22:00:00Z"})
            rows = db.get_all()
            assert len(rows) == 2
            db.close()
        finally:
            os.unlink(db_path)
