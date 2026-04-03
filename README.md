# polymarket-arb

Automated sports market making bot for **Polymarket US** and **Kalshi**. Quotes both sides of pre-game sports markets (NBA, NHL, MLB, NCAA spreads/totals), captures the bid-ask spread, and exits all positions before game start.

## Strategy

- **Pre-game market making**: OBI microprice, continuous inventory skew, dynamic volatility-based spread
- **Pre-game only**: exit all positions before live game starts (time-based + frequency-based detection)
- **Dual platform**: Polymarket US (live) + Kalshi (paper)
- **4-layer risk management**: per-order validation, inventory limits, session P&L gates, system circuit breakers

## Architecture

```
scripts/poly_daily_scan.py       Market scanner (events API, rank-based scoring)
scripts/poly_live_mm.py          Live market maker (real orders via Polymarket SDK)
scripts/poly_paper_mm.py         Paper trading (simulated fills)

src/poly_client.py               Polymarket US API client
src/kalshi_client.py             Kalshi API client (RSA-PSS auth)
src/mm/engine.py                 Market making engine (10s tick loop)
src/mm/state.py                  OBI microprice, skewed quotes, dynamic spread
src/mm/risk.py                   4-layer risk management (L1-L4)
src/mm/db.py                     SQLite persistence (fills, orders, snapshots)
```

### Data Flow

```
Scanner selects markets
  → Engine quotes both sides (YES + NO)
  → Fills detected via portfolio.activities() API
  → Inventory skew adjusts quotes
  → Pre-game exit on game start detection
```

### Fill Detection

Fill detection uses `portfolio.activities()` for exchange-confirmed trade data:
- Session watermark (ignores pre-session trades)
- Passive-only filter (maker fills, not taker)
- Price-matched to tracked orders
- Trade ID dedup (no double-counting)

## Risk Management

| Layer | Scope | Controls |
|-------|-------|----------|
| L1 | Per-order | Fat-finger check (price within 10% of mid), max 5 contracts |
| L2 | Inventory | Continuous skew (gamma=0.5c), single-side cap at 10, time-based flatten |
| L3 | Session P&L | Daily loss limit $5, consecutive loss pause, per-market exit at -$10 |
| L4 | System | Price jump detection, crossed book skip, API disconnect cancel, pre-game exit |

## Setup

```bash
git clone https://github.com/charlieyang1557/polymarket-arb.git
cd polymarket-arb
pip install -r requirements.txt
cp .env.example .env  # add Polymarket API credentials
```

## Usage

```bash
# Daily scanner — find tradeable markets
python scripts/poly_daily_scan.py --max-markets 5 --max-check 50

# Paper trading
python scripts/poly_paper_mm.py --slugs SLUG1,SLUG2 --duration 86400

# Live trading (real orders, $25 bankroll)
python scripts/poly_live_mm.py --slugs SLUG1,SLUG2 --capital 2500 --size 2 --interval 10

# Dry run (previews orders without submitting)
python scripts/poly_live_mm.py --dry-run --slugs SLUG1,SLUG2 --capital 2500

# Tests
python -m pytest tests/test_poly_live_mm.py -q
```

## Platforms

| Platform | Auth | Status | Notes |
|----------|------|--------|-------|
| Polymarket US | SDK key/secret | Live | Sports markets, maker rebates |
| Kalshi | RSA-PSS signatures | Paper | CFTC-regulated, event contracts |

## Key Design Decisions

- **Cross-tick losses are stop-losses, not bugs**: negative round-trips (YES+NO > 100c) are natural stop-losses when market moves between fills
- **Pre-game only**: 10s polling can't compete with sub-second HFT during live events
- **Spread P&L is the edge**: directional profits from inventory are luck, not repeatable
- **Activities-based fill detection**: order-disappearance inference was abandoned after persistent phantom fill bugs from slug remap issues

---

> **Disclaimer**: For educational and research purposes. Trading involves risk of loss.
