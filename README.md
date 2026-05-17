# polymarket-arb

Automated trading system for **Polymarket US** and **Kalshi** prediction markets. Built, deployed live, and systematically tested four strategies (Mar 10 – May 2026). Informed by [Bartlett & O'Hara (2026)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739) "Adverse Selection in Prediction Markets: Evidence from Kalshi" — the academic foundation for understanding why retail market makers lose on prediction markets.

**[Full Project Report](https://htmlpreview.github.io/?https://github.com/charlieyang1557/polymarket-arb/blob/main/docs/polymarket-project-report.html)**

## Results

| Metric | Value |
|--------|-------|
| Duration | Mar 10 – May 16, 2026 |
| Capital deployed | $28.03 |
| Balance (final) | $28.68 (+$0.65) |
| Live fills | 36 maker fills across 5 sessions |
| Strategies tested | 4 (all negative or neutral EV) |
| Commits | ~190 |
| Unit tests | 656 across 37 test files |

**Key finding**: Polymarket US sports markets exhibit the same adverse selection dynamics described in Bartlett & O'Hara — YES-biased uninformed takers cross-subsidize informed flow, but only when makers are on the right side of that asymmetry. Our bot absorbed 1.79x more YES fills (the losing side), driving a ~26% round-trip fill rate. Six targeted mitigations were deployed (fair-value anchoring, adaptive gamma, near-touch OBI, fee model fix, priority quoting, kill condition tracker) but the structural disadvantage persists at retail scale.

## Adverse Selection: The Core Problem

Bartlett & O'Hara (2026) show that on Kalshi, makers earn +1.91c/contract on single-name markets — not from the bid-ask spread, but from a **frequency edge**: YES-biased uninformed takers systematically overbet YES on markets that mostly settle NO. The behavioral tax these takers pay ($18.22M on NO-settling markets) cross-subsidizes informed trader losses ($11.97M).

Our live data confirmed the same mechanism on Polymarket sports:
- **yes_bid fills** lost -4.62c/contract (WR 43.9%) — the paper's losing side
- **no_bid fills** won +2.31c/contract (WR 51.3%) — the paper's profitable side
- **Fill ratio**: 209 YES / 117 NO (1.79x asymmetry toward the losing side)

The round-trip simulator on 326 live fills showed that a flat 1c YES penalty (to reduce adverse fills) is net mildly negative in aggregate (-$1.56), though it helps on some market types (tsc: +$0.32) and hurts on others (aec: -$17.69) due to drift-correlated exit pricing.

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
# Market Making (live trading)
scripts/poly_live_mm.py          Live MM engine (real orders via Polymarket SDK)
scripts/poly_paper_mm.py         Paper trading (simulated fills)
scripts/poly_daily_scan.py       Market scanner (events API, rank-based scoring)

# Research Tools
scripts/research/roundtrip_simulator.py  Round-trip fill simulator (326 live fills, survival model)
scripts/cross_market_logger.py           30s orderbook snapshots across correlated markets
scripts/analyze_cross_market.py          Lag detection, direction accuracy, simulated PnL
scripts/poly_calibration.py              Pinnacle de-vig odds comparison

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
- **Round-trip simulator**: Survival-model-based fill pair simulator on 326 live fills. Tests strategy modifications (YES penalty, differential by market type) against actual production data with drift-correlated exit pricing.
- **Kill condition tracker**: Records session stats and alerts if round-trip rate stays below 35% over 5 sessions — automated strategy sunset gate.
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

> **Disclaimer**: For educational and research purposes. Trading involves risk of loss. This project concluded that retail-scale market making on prediction markets faces structural adverse selection that is difficult to overcome without the frequency edge described in Bartlett & O'Hara (2026).
