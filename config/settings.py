RISK_CONFIG = {
    # Position limits
    "max_single_trade_usd": 20,
    "max_position_per_market_usd": 50,
    "max_total_exposure_usd": 200,

    # Minimum edge to trade
    "min_profit_type1_pct": 1.0,   # Type 1: min 1% profit after fees
    "min_profit_type2_pct": 3.0,   # Type 2: min 3% edge (higher risk)

    # Stop-loss
    "daily_loss_limit_usd": 20,
    "consecutive_loss_limit": 3,
    "max_drawdown_pct": 10,

    # Circuit breakers
    "max_trades_per_hour": 20,
    "cooldown_after_loss_minutes": 30,

    # Liquidity requirements
    "min_book_depth_usd": 100,
    "max_slippage_pct": 0.5,
}

# API endpoints
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"

# Scanner settings
SCAN_INTERVAL_SECONDS = 30
METADATA_CACHE_TTL_SECONDS = 300  # 5 min

# Polymarket US fee
TRADE_FEE_PCT = 0.0001  # 0.01%
