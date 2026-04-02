"""Event matching: map The-Odds-API events to Polymarket slugs.

Polymarket slug format: prefix-sport-team1-team2-YYYY-MM-DD[-detail]
Examples:
  aec-nba-bos-mia-2026-04-01          (moneyline)
  asc-nba-bos-mia-2026-04-01-pos-5pt5 (spread)
  tsc-nba-bos-mia-2026-04-01-238pt5   (total)

The-Odds-API provides: home_team="Boston Celtics", away_team="Miami Heat"
"""

import re
from datetime import datetime, timezone

# Sport key → slug sport code
SPORT_TO_SLUG = {
    "basketball_nba": "nba",
    "basketball_ncaab": "cbb",
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "cfb",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "mma_mixed_martial_arts": "ufc",
}

# Full team name → 3-letter abbreviation used in Polymarket slugs
# This is the core lookup table — extend as needed
_TEAM_ABBREVS: dict[str, str] = {
    # NBA
    "atlanta hawks": "atl", "boston celtics": "bos", "brooklyn nets": "bkn",
    "charlotte hornets": "cha", "chicago bulls": "chi",
    "cleveland cavaliers": "cle", "dallas mavericks": "dal",
    "denver nuggets": "den", "detroit pistons": "det",
    "golden state warriors": "gsw", "houston rockets": "hou",
    "indiana pacers": "ind", "la clippers": "lac",
    "los angeles clippers": "lac", "la lakers": "lal",
    "los angeles lakers": "lal", "memphis grizzlies": "mem",
    "miami heat": "mia", "milwaukee bucks": "mil",
    "minnesota timberwolves": "min", "new orleans pelicans": "nop",
    "new york knicks": "nyk", "oklahoma city thunder": "okc",
    "orlando magic": "orl", "philadelphia 76ers": "phi",
    "phoenix suns": "phx", "portland trail blazers": "por",
    "sacramento kings": "sac", "san antonio spurs": "sas",
    "toronto raptors": "tor", "utah jazz": "uta",
    "washington wizards": "was",
    # NFL
    "arizona cardinals": "ari", "atlanta falcons": "atl",
    "baltimore ravens": "bal", "buffalo bills": "buf",
    "carolina panthers": "car", "chicago bears": "chi",
    "cincinnati bengals": "cin", "cleveland browns": "cle",
    "dallas cowboys": "dal", "denver broncos": "den",
    "detroit lions": "det", "green bay packers": "gb",
    "houston texans": "hou", "indianapolis colts": "ind",
    "jacksonville jaguars": "jax", "kansas city chiefs": "kc",
    "las vegas raiders": "lv", "los angeles chargers": "lac",
    "los angeles rams": "lar", "miami dolphins": "mia",
    "minnesota vikings": "min", "new england patriots": "ne",
    "new orleans saints": "no", "new york giants": "nyg",
    "new york jets": "nyj", "philadelphia eagles": "phi",
    "pittsburgh steelers": "pit", "san francisco 49ers": "sf",
    "seattle seahawks": "sea", "tampa bay buccaneers": "tb",
    "tennessee titans": "ten", "washington commanders": "was",
    # NHL
    "anaheim ducks": "ana", "boston bruins": "bos",
    "buffalo sabres": "buf", "calgary flames": "cgy",
    "carolina hurricanes": "car", "chicago blackhawks": "chi",
    "colorado avalanche": "col", "columbus blue jackets": "cbj",
    "dallas stars": "dal", "detroit red wings": "det",
    "edmonton oilers": "edm", "florida panthers": "fla",
    "los angeles kings": "la", "minnesota wild": "min",
    "montreal canadiens": "mtl", "nashville predators": "nsh",
    "new jersey devils": "njd", "new york islanders": "nyi",
    "new york rangers": "nyr", "ottawa senators": "ott",
    "philadelphia flyers": "phi", "pittsburgh penguins": "pit",
    "san jose sharks": "sj", "seattle kraken": "sea",
    "st louis blues": "stl", "st. louis blues": "stl",
    "tampa bay lightning": "tb", "toronto maple leafs": "tor",
    "utah hockey club": "uta", "vancouver canucks": "van",
    "vegas golden knights": "vgk", "washington capitals": "was",
    "winnipeg jets": "wpg",
    # MLB
    "arizona diamondbacks": "ari", "atlanta braves": "atl",
    "baltimore orioles": "bal", "boston red sox": "bos",
    "chicago cubs": "chc", "chicago white sox": "chw",
    "cincinnati reds": "cin", "cleveland guardians": "cle",
    "colorado rockies": "col", "detroit tigers": "det",
    "houston astros": "hou", "kansas city royals": "kc",
    "los angeles angels": "laa", "los angeles dodgers": "lad",
    "miami marlins": "mia", "milwaukee brewers": "mil",
    "minnesota twins": "min", "new york mets": "nym",
    "new york yankees": "nyy", "oakland athletics": "oak",
    "philadelphia phillies": "phi", "pittsburgh pirates": "pit",
    "san diego padres": "sd", "san francisco giants": "sf",
    "seattle mariners": "sea", "st louis cardinals": "stl",
    "st. louis cardinals": "stl", "tampa bay rays": "tb",
    "texas rangers": "tex", "toronto blue jays": "tor",
    "washington nationals": "was",
}

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def normalize_team(name: str) -> str:
    """Normalize a full team name to its Polymarket slug abbreviation."""
    key = name.lower().strip()
    if key in _TEAM_ABBREVS:
        return _TEAM_ABBREVS[key]

    # Fallback: try last word (team name without city)
    parts = key.split()
    for i in range(len(parts)):
        suffix = " ".join(parts[i:])
        if suffix in _TEAM_ABBREVS:
            return _TEAM_ABBREVS[suffix]

    # Last resort: first 3 chars of last word
    return parts[-1][:3] if parts else key[:3]


def extract_teams_from_slug(slug: str) -> tuple[str, str] | None:
    """Extract team abbreviations from a Polymarket slug.

    Returns (team1, team2) or None if unparseable.
    Slug format: prefix-sport-team1-team2-YYYY-MM-DD[-extras]
    """
    parts = slug.split("-")
    if len(parts) < 5:
        return None

    # Find date position
    date_match = _DATE_RE.search(slug)
    if not date_match:
        return None

    date_str = date_match.group(1)
    date_parts = date_str.split("-")  # ["2026", "04", "01"]

    # Find the index of the first date component in parts
    try:
        year_idx = parts.index(date_parts[0])
    except ValueError:
        return None

    # Teams are between sport (index 1) and date
    team_parts = parts[2:year_idx]
    if len(team_parts) < 2:
        return None

    # Usually exactly 2 parts: team1, team2
    # But some teams have multi-part abbrevs (rare for Polymarket)
    return (team_parts[0].lower(), team_parts[1].lower())


def match_event_to_slugs(home_team: str, away_team: str, sport: str,
                         commence_time: str,
                         slugs: list[str]) -> str | None:
    """Match an Odds-API event to the best Polymarket slug.

    Returns the matching slug, or None if no match found.
    """
    home_abbr = normalize_team(home_team)
    away_abbr = normalize_team(away_team)
    sport_code = SPORT_TO_SLUG.get(sport, "")

    # Extract date from commence_time
    try:
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        event_date = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        event_date = ""

    best_match = None
    best_score = 0

    for slug in slugs:
        slug_lower = slug.lower()

        # Sport code must match
        if sport_code and f"-{sport_code}-" not in slug_lower:
            continue

        # Date should match
        if event_date and event_date not in slug_lower:
            continue

        teams = extract_teams_from_slug(slug)
        if teams is None:
            continue

        t1, t2 = teams

        # Check team match (either order)
        score = 0
        if (t1 == home_abbr and t2 == away_abbr):
            score = 3  # exact order match
        elif (t1 == away_abbr and t2 == home_abbr):
            score = 3  # reversed order (away@home)
        elif home_abbr in slug_lower and away_abbr in slug_lower:
            score = 2  # both teams present
        elif home_abbr in slug_lower or away_abbr in slug_lower:
            score = 1  # partial match

        # Prefer moneyline (aec prefix) over spreads/totals
        if slug_lower.startswith("aec-"):
            score += 0.5

        if score > best_score:
            best_score = score
            best_match = slug

    return best_match if best_score >= 2 else None
