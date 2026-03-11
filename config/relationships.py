"""
Manual relationship mappings for Type 2 logical arbitrage.
Each rule defines a detected logical relationship between two markets.
"""

RELATIONSHIP_RULES = [
    {
        "id": "wins_championship_implies_conference",
        "description": "If team X wins championship, X's conference wins",
        "example": "Chiefs win Super Bowl → AFC wins Super Bowl",
        "category": "sports",
        "direction": "m1_implies_m2",  # m1 happening means m2 must happen
    },
    {
        "id": "wins_division_implies_makes_playoffs",
        "description": "If team X wins division, X makes playoffs",
        "category": "sports",
        "direction": "m1_implies_m2",
    },
    {
        "id": "fed_cuts_50bp_implies_cuts_rates",
        "description": "Fed cuts 50bp implies Fed cuts rates (any amount)",
        "category": "macro",
        "direction": "m1_implies_m2",
    },
    {
        "id": "before_june_implies_before_december",
        "description": "Event before June implies event before December",
        "category": "temporal",
        "direction": "m1_implies_m2",
    },
    {
        "id": "threshold_higher_implies_lower",
        "description": "Price > $100k implies price > $90k",
        "category": "price_threshold",
        "direction": "m1_implies_m2",
    },
]
