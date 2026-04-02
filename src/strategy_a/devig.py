"""De-vigging module: extract fair probabilities from bookmaker odds.

Implements multiplicative de-vigging (simplest, good enough for 2-way markets).
Pinnacle overround is typically 2-4% on major sports.
"""


def american_to_decimal(american: int | float) -> float:
    """Convert American odds to decimal odds.

    American: -150 means bet $150 to win $100
              +200 means bet $100 to win $200
    Decimal: includes the stake (1.67 means $1 returns $1.67)
    """
    american = float(american)
    if american >= 100:
        return 1 + american / 100
    elif american <= -100:
        return 1 + 100 / abs(american)
    else:
        # Between -100 and +100 (invalid in standard American format)
        # Treat as decimal-like
        return 2.0


def devig_decimal(home_odds: float, away_odds: float) -> tuple[float, float, float]:
    """Multiplicative de-vig for 2-way market.

    Args:
        home_odds: decimal odds for home team (e.g., 1.85)
        away_odds: decimal odds for away team (e.g., 2.05)

    Returns:
        (fair_home_prob, fair_away_prob, vig)
        where fair probs sum to 1.0 and vig is the overround.

    Raises:
        ValueError: if odds are <= 1.0 (invalid)
    """
    if home_odds <= 1.0 or away_odds <= 1.0:
        raise ValueError(
            f"Invalid odds: home={home_odds}, away={away_odds}. "
            f"Decimal odds must be > 1.0")

    implied_home = 1.0 / home_odds
    implied_away = 1.0 / away_odds
    total = implied_home + implied_away
    vig = total - 1.0

    fair_home = implied_home / total
    fair_away = implied_away / total

    return (round(fair_home, 6), round(fair_away, 6), round(vig, 6))
