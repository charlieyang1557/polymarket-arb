# polymarket-arb

Automated trading system for **Polymarket US** and **Kalshi** prediction markets. Built, deployed live, and systematically tested four strategies over 12 days (Mar 26 – Apr 7, 2026). Concluded that no retail-accessible edge exists at our capital scale.

**[Full Project Report](docs/polymarket-project-report.html)**

## Results

| Metric | Value |
|--------|-------|
| Duration | 28 days (Mar 10 – Apr 7, 2026) |
| Capital deployed | $28.03 |
| Final balance | $28.68 (+$0.65) |
| Live fills | 36 maker fills across 5 sessions |
| Strategies tested | 4 (all concluded negative or neutral EV) |
| Commits | 131 |
| Unit tests | 596 across 36 test files |

**Key finding**: Polymarket US sports markets are priced within ±0.8% of Pinnacle (the world's sharpest sportsbook). No retail-accessible edge exists in passive market making, directional taker strategies, or cross-market correlation trading at $25-30 capital.

## Strategies Tested

### 1. Pre-Game Passive Market Making — Negative EV
Quote both sides of pre-game sports markets, capture bid-ask spread, earn maker rebates. **Result**: adverse selection from informed flow wiped out spread capture. Round-trips completed at a loss due to market moves between fills.

### 2. Odds Calibration (Pinnacle De-Vig) — No Edge
Compare Polymarket prices against de-vigged Pinnacle lines to find mispriced markets. **Result**: prices converge within ±0.8% — no systematic mispricing to exploit.

### 3. Cross-Market Correlation — No Edge
When a moneyline reprices, do correlated spread/totals markets lag? **Result**: direction accuracy ~50% (coin flip), negative simulated PnL across all sports and pair types.

### 4. WebSocket Event Trading — Abandoned
Monitor real-time market events for momentum signals. Abandoned after Strategy 1-3 results showed no exploitable inefficiency.

## Architecture

```
# Market Making (concluded — code preserved)
scripts/poly_live_mm.py          Live MM engine (real orders via Polymarket SDK)
scripts/poly_paper_mm.py         Paper trading (simulated fills)
scripts/poly_daily_scan.py       Market scanner (events API, rank-based scoring)

# Research Tools
scripts/cross_market_logger.py   30s orderbook snapshots across correlated markets
scripts/analyze_cross_market.py  Lag detection, direction accuracy, simulated PnL
scripts/poly_calibration.py      Pinnacle de-vig odds comparison

# Core Engine (shared)
src/poly_client.py               Polymarket US API adapter
src/kalshi_client.py             Kalshi API client (RSA-PSS auth)
src/mm/engine.py                 Market making engine (10s tick loop)
src/mm/state.py                  OBI microprice, skewed quotes, dynamic spread
src/mm/risk.py                   4-layer risk management (L1-L4)
src/mm/db.py                     SQLite persistence (fills, orders, snapshots)
```

## Risk Management

| Layer | Scope | Controls |
|-------|-------|----------|
| L1 | Per-order | Fat-finger check (±10% of mid), max contract size |
| L2 | Inventory | Continuous skew (gamma=0.5c), single-side cap, time-based flatten |
| L3 | Session P&L | Daily loss limit $5, consecutive loss pause, per-market exit at -$10 |
| L4 | System | SOFT_CLOSE at 15min pre-game, EXIT_MARKET at game start, API disconnect cancel-all |

## Technical Highlights

- **Cancel-pending state machine**: Prevents duplicate order placement during exchange poll lag. Cancel marks `cancel_pending` in local tracking; placement waits for poll confirmation.
- **Activities-based fill detection**: Exchange-confirmed fills via `portfolio.activities()` with session watermark, passive-only filter, and trade ID dedup.
- **Cross-market correlation analysis**: 350K+ orderbook snapshots across 59 events, with direction accuracy and simulated PnL including price-dependent taker fees and T+1 execution (no lookahead bias).

## Setup

```bash
git clone https://github.com/charlieyang1557/polymarket-arb.git
cd polymarket-arb
pip install -r requirements.txt
cp .env.example .env  # add API credentials
```

## Tests

```bash
python -m pytest tests/ -q
```

---

> **Disclaimer**: For educational and research purposes. Trading involves risk of loss. This project concluded that the tested strategies are not profitable at retail scale.
