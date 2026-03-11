# polymarket-arb

Automated scanner and trader that detects arbitrage opportunities on Polymarket.

## Architecture

```
MAIN LOOP (every 30s)
    ├── RebalanceScanner (Type 1)   — sum of all YES prices < $1 in a neg-risk event
    └── LogicalScanner (Type 2)    — logically related markets with inconsistent prices
            ↓
    OpportunityEvaluator           — fee calc, liquidity check, min edge filter
            ↓
    RiskManager                    — position limits, stop-loss, circuit breakers
            ↓
    PaperTrader (Phase 1-2) / LiveTrader (Phase 3+)
            ↓
    Dashboard + SQLite logging
```

## Setup

```bash
git clone https://github.com/charlieyang1557/polymarket-arb.git
cd polymarket-arb
pip install -r requirements.txt
cp .env.example .env   # fill in keys only for Phase 3+ live trading
```

## Usage

```bash
# One-shot scan (no trades)
python scripts/scan_once.py

# Paper trading loop
python main.py

# Live trading (Phase 3+, requires API keys in .env)
python main.py --live

# Export trade history
python scripts/export_trades.py --output trades.csv
```

## Phase Roadmap

| Phase | Mode | Goal |
|-------|------|------|
| 1 — Scanner | Read-only | Measure opportunity frequency & size |
| 2 — Paper trading | Simulated | Validate strategy P&L |
| 3 — Small live ($100–200) | Real | Discover real-world execution issues |
| 4 — Scale up ($500–1,000) | Real | Optimize, add WebSocket, Type 2 embeddings |

## Key Config

All risk parameters are in `config/settings.py`:
- Max trade size: $20 | Max total exposure: $200
- Min edge: 1% (Type 1), 3% (Type 2)
- Daily loss limit: $20 | Max drawdown: 10%

## Polymarket Notes

- **Neg-risk markets**: multi-outcome mutually exclusive events (Type 1 targets)
- **Fee**: 0.01% per trade on Polymarket US (use limit/maker orders for 0 fee)
- **No shorting**: can only buy YES or NO tokens
- **Settlement**: profit realized when market resolves (days to months)
- **KYC required**: set up account via Polymarket iOS app before live trading
- **API keys**: Ed25519 format, generated via `py-clob-client`

---

> **Disclaimer:** For educational purposes. Trading involves risk of loss.
