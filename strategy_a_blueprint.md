# Strategy A: Systematic No Bias — Blueprint & Roadmap
**Date:** 2026-04-01 | **Author:** Claude (strategic advisor) + Charlie (owner/operator)
**Repo:** `~/polymarket-arb` | **Branch:** `feature/strategy-a`

---

## 0. Why We're Here

Two days of live market making on Polymarket US produced zero fills.
Root cause: 70%+ of markets have 1c spread, institutional makers dominate
queue priority, and our 2-contract orders can't compete.

**Strategic pivot:** From passive maker (competing on queue speed) to
directional taker (competing on probability estimation accuracy).

---

## 1. Strategy Overview

**Core Thesis:** Prediction markets systematically misprice certain outcomes.
By identifying and exploiting these mispricings, we can generate positive
expected value (EV) as a taker.

**Mechanism:**
1. Estimate "true" probability of an outcome using alpha factors
2. Compare our estimate to market price (implied probability)
3. If edge > threshold (after fees), place taker order
4. Wait for settlement, collect profit on correct predictions

**Key Difference from MM:**
- MM earns spread, needs queue priority, high frequency, low edge per trade
- Strategy A earns prediction accuracy, needs no queue, low frequency, higher edge per trade

---

## 2. Data Assets

### What We Have
- **930 resolved Polymarket markets** from calibration study
  - Fields (to verify): slug, market_type, sport, settlement (0 or 1),
    last_price or closing_price, possibly volume/liquidity metrics
  - Source: `scripts/kalshi_calibration_category.py` output or
    Gamma API historical data
  - Location: likely `data/` directory or needs re-fetching

### What We Need (Later Phases)
- Historical orderbook snapshots at specific time-before-game intervals
- External sportsbook odds (Pinnacle, DraftKings) via The-Odds-API
- Real-time Polymarket prices for live signal generation

### Data Quality Issues to Handle
- **Void/Push/Canceled markets:** Filter out any market not settled as
  clean 0 or 1 (e.g., rained-out games, exact-spread pushes)
- **Snapshot timestamp inconsistency:** Document what price we're using
  (last traded? closing? settlement?) and note limitations
- **Sport/type distribution:** Count samples per bucket before sub-analysis;
  minimum 30 per bucket for any statistical claim

---

## 3. Phase Plan

### Phase 1: Exploratory Data Analysis + Calibration (1-2 days)
**Goal:** Answer "Is there systematic bias in Polymarket pricing?"

**Input:** 930 resolved markets (re-fetch if needed via Gamma API)

**Steps:**
1. Load and inspect data — what fields do we actually have?
2. Clean data — remove void/push, remove non-sports, remove duplicates
3. Calibration curve — 10 price buckets, actual win rate vs implied prob
4. Brier Score comparison — market vs naive strategies
5. Simple PnL simulation — "buy all NO below 50c" with fee + slippage
6. Sport breakdown — if sample size permits (n≥30 per group)

**Output:**
- `calibration_curve.png` — the single most important chart
- `phase1_report.json` — bucket stats, brier scores, simulated PnL
- Console summary with key findings

**Key Metrics:**
| Metric | What It Tells Us |
|--------|-----------------|
| Calibration curve slope | >1 = favorites overpriced, <1 = underdogs overpriced |
| Brier Score (market) | Baseline — how good is the market at predicting? |
| Brier Score (model) | Our model — is it better than market? |
| Simulated PnL (post-fee) | Bottom line — does the bias survive transaction costs? |
| Sample count per bucket | Statistical reliability of each bucket's signal |

**Fee Assumptions:**
- Taker fee: 1.75% × p × (1-p) where p = price (Polymarket US formula)
- Slippage: +1c conservative (we're taking best ask/bid)
- Total cost per trade: ~2-3c depending on price level

**Decision Gate:**
- If calibration shows NO systematic bias → Strategy A is dead, pivot to C
- If bias exists but <2% after fees → Edge too thin, not tradeable
- If bias exists and >3% after fees → Proceed to Phase 2

---

### Phase 2: Backtest Engine (2-3 days)
**Goal:** Build reusable backtest framework for testing alpha factors

**Prerequisites:** Phase 1 confirms exploitable bias exists

**Architecture:**
```
backtest/
├── data_loader.py      — Load resolved markets, clean, normalize
├── alpha_signals.py    — Alpha factor functions (pluggable)
├── simulator.py        — Position sizing, PnL tracking, fee model
├── metrics.py          — Brier, Sharpe, drawdown, win rate, profit factor
├── visualizer.py       — Calibration curves, PnL charts, factor analysis
└── run_backtest.py     — CLI entry point
```

**Alpha Factors to Test (Priority Order):**

| # | Factor | Data Needed | Complexity |
|---|--------|-------------|-----------|
| 1 | Price bucket bias | Settlement + price | Trivial |
| 2 | Extreme price mean reversion | Settlement + price | Low |
| 3 | Sport-specific bias | Settlement + price + sport | Low |
| 4 | Market type bias (ML/spread/total) | Settlement + price + type | Low |
| 5 | Volume-weighted bias | Settlement + price + volume | Medium |
| 6 | Cross-market consistency | Multiple markets per event | Medium |
| 7 | External odds comparison | Sportsbook API data | High (Phase 3) |

**Backtest Output Per Strategy:**
- Cumulative PnL curve (post-fees)
- Win rate, profit factor, max drawdown
- Brier Score vs market baseline
- Number of trades, avg edge per trade
- Statistical significance (p-value or confidence interval)

**Position Sizing (Simple for now):**
- Fixed $1 per signal (MVP)
- Kelly Criterion (Phase 4 optimization)

**Overfitting Prevention:**
- Train/test split: first 70% of markets for discovery, last 30% for validation
- Report both in-sample and out-of-sample metrics
- No factor should be adopted unless OOS Sharpe > 0.5

---

### Phase 3: External Data Integration (1 week)
**Goal:** Add sportsbook odds as strongest alpha signal

**Prerequisites:** Phase 2 confirms internal factors have some edge

**Data Source:** The-Odds-API (free tier: 500 requests/month)
- Historical and live odds from Pinnacle, DraftKings, FanDuel
- Covers: NFL, NBA, MLB, NHL, NCAA, MMA, Tennis

**Key Implementation:**
1. Odds fetcher — pull closing lines for historical events
2. De-vigging module — Shin's Method to extract fair probability
   ```
   Raw Pinnacle: Team A = 1.85, Team B = 2.05
   Implied: 54.1% + 48.8% = 102.9% (2.9% vig)
   De-vigged fair: ~52.5% vs ~47.5%
   ```
3. Cross-platform signal — `edge = devigged_pinnacle_prob - polymarket_price`
4. Backtest this signal using Phase 2 engine

**Expected Result:** This should be the strongest single factor.
Academic literature consistently shows bookmaker closing lines are
the most efficient probability estimators in sports.

---

### Phase 4: Live Signal Generation + Execution (1 week)
**Goal:** Generate real-time trade signals and execute on Polymarket US

**Prerequisites:** Phase 3 confirms combined alpha > fees after OOS validation

**Components:**
1. **Signal scanner** — runs every N minutes, checks all active markets
   against alpha model, outputs ranked opportunities with edge estimate
2. **Execution module** — places taker orders on Polymarket US
   - Uses existing `poly_client.py` authenticated endpoints
   - `participateDontInitiate=False` (we WANT taker fills now)
   - Order size from Kelly or fixed sizing
3. **Risk management:**
   - Max daily loss limit
   - Max exposure per event
   - Max concurrent positions
   - Correlation check (don't bet both sides of same game via
     different market types)
4. **Monitoring** — Discord alerts for every trade, daily P&L summary

**Capital Deployment Ladder:**
| Stage | Capital | Criteria to Advance |
|-------|---------|-------------------|
| Paper | $0 | Backtest Sharpe > 1.0 OOS |
| Pilot | $25 | 50+ live trades, positive PnL |
| Scale 1 | $100 | 2 weeks profitable, drawdown < 10% |
| Scale 2 | $500 | 1 month profitable, Sharpe > 0.5 live |

---

## 4. Technical Decisions

### What We Reuse from MM Bot
- `src/poly_client.py` — API wrapper (add taker order method)
- `poly_daily_scan.py` — market discovery (modify filters for taker strategy)
- Discord webhook alerts
- Mac Mini production infrastructure
- SQLite for trade logging

### What We Build New
- Backtest engine (Phase 2)
- Alpha signal library (Phase 2-3)
- De-vigging module (Phase 3)
- Live signal scanner (Phase 4)
- Taker execution logic (Phase 4)

### What We Explicitly Do NOT Build
- ML/deep learning models (not until we have 5000+ samples)
- Real-time orderbook reconstruction
- Sub-second execution latency
- Multi-platform arbitrage engine
- Custom UI/dashboard (Discord + CLI sufficient)

---

## 5. Risk Management Philosophy

**MM Bot Lesson Learned:** Paper trading was 100% disconnected from live
reality. For Strategy A, we must ensure backtest → live gap is minimal.

**How We Close the Gap:**
1. All backtests include realistic fees + slippage
2. OOS validation mandatory before any live deployment
3. Capital deployment ladder forces gradual scaling
4. Every live trade logged to SQLite for post-hoc vs backtest comparison

**Kill Conditions:**
- Phase 1 shows no bias > 2% → Kill Strategy A entirely
- Phase 2 OOS Sharpe < 0.5 → Do not proceed to live
- Phase 4 pilot loses > $10 in first 50 trades → Pause, re-evaluate
- Live Brier Score worse than market for 100+ trades → Model is broken

---

## 6. Key Learnings from MM Bot (Applied Here)

| MM Lesson | How It Applies to Strategy A |
|-----------|----------------------------|
| Paper sim ≠ live reality | Backtest must include all real costs |
| Data first, complexity second | Phase 1 = pure data analysis, no models |
| Incremental validation | Phase gates: each phase must pass before next |
| Root cause discipline | If backtest looks good but live doesn't, dig into WHY |
| Over-engineering kills velocity | No ML until proven with simple stats |
| Sample size matters | n≥30 per bucket, OOS validation mandatory |

---

## 7. Success Criteria

**Phase 1 Success:** Clear calibration curve showing ≥3% bias in at least
one price bucket, surviving fee deduction.

**Phase 2 Success:** Backtest showing Sharpe > 1.0 in-sample AND > 0.5
out-of-sample, with >50 simulated trades.

**Phase 3 Success:** External odds signal improves Sharpe by ≥0.3 over
internal-only factors.

**Phase 4 Success:** 50+ live trades with positive PnL after fees,
live Brier Score < market Brier Score.

**Overall Success:** Consistent $1-5/day profit on $100-500 capital
with < 15% max drawdown.
