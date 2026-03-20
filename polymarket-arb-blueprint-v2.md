# Polymarket Arbitrage Bot — Blueprint v2 (Strategy Pivot)

## What Changed and Why

Type 1 (multi-option rebalancing) diagnostic results:
- 8,020 events scanned → 2,118 neg-risk events with 3+ outcomes
- 309 appeared to have sum < $1.00
- After exhaustiveness filter: only 8 had catch-all outcomes (the rest were false positives)
- Best real edge: ~4% on exhaustive markets, ~10% on a couple of outliers (Gamma midpoint, not CLOB ask)
- 4% edge with execution risk + capital lockup ≈ risk-adjusted return worse than bank deposits (3.25%)
- Root cause: market is already efficient — existing bots have compressed Type 1 edges to near-zero

**Decision:** Pivot to higher-edge strategies. Keep Type 1 scanner as low-priority background.

### Lessons Learned from Diagnostic Phase

These findings apply to ALL future strategies:

1. **Gamma outcomePrices ≈ midpoint, not ask.** Always verify with CLOB /price?side=SELL for actual cost.
2. **clobTokenIds is a JSON-encoded string**, not a list. Must json.loads() before using.
3. **volume field is volumeNum**, not volume24hr (which is None).
4. **side=BUY returns best bid, side=SELL returns best ask.** Counter-intuitive but confirmed.
5. **Neg-risk order books are synthetic.** Raw token-level books show 0.001/0.999 spreads; /price endpoint returns meaningful synthetic prices computed across the neg-risk framework.
6. **Most multi-outcome events are NOT exhaustive.** Without "Other/Field" catch-all markets, sum(YES) < $1 is expected, not an arbitrage opportunity.
7. **Rate limits: ~60 req/min effective.** Full scan of 15k tokens takes hours via CLOB. Use Gamma outcomePrices (free, instant) for initial filtering, CLOB only for verification.
8. **Dead tokens are common.** ~20-25% of listed tokens return 404. Cache dead token IDs to avoid wasted API calls.

---

## New Strategy Focus

### Strategy A: Systematic "No" Bias (PRIMARY — Backtest Phase)

This strategy has two distinct sub-hypotheses:

#### A1: Psychological Bias — Retail Optimism (Backtest First)

**Thesis:** In hype-driven market categories, retail traders systematically overvalue YES due to optimism bias, wishful thinking, and narrative-driven betting. Buying NO in these markets should have positive expected value over a large sample.

**Target categories (from community research):**
- **Crypto/DeFi:** FDV predictions, token launch targets, "will X coin hit $Y" — retail is structurally bullish
- **Airdrops/Pre-Market:** "Will airdrop exceed $X" — speculators overpay for upside
- **Celebrity/Entertainment:** Fan enthusiasm inflates YES (awards predictions, "will X do Y")
- **Sports championships:** Fan loyalty inflates preseason favorites

**Control groups (likely well-calibrated):**
- **Politics:** Heavy sophisticated money, probably efficient
- **Binary outcomes with clear data:** Weather, economic indicators

**Why this works for us:**
- No speed requirement — statistical edge, not latency
- Works with small capital ($100-200), compounds over many trades
- Can be fully backtested before risking money
- Edge comes from human psychology — harder for bots to eliminate
- Community reports confirm this pattern exists in specific categories

**Success criteria before going live:**
- At least 1 category where buying NO at YES > 0.65 has > 55% historical win rate
- Minimum 100 resolved markets in that category for statistical significance
- Expected value per trade > 5% (after accounting for Polymarket fees)
- p-value < 0.05 on the win rate deviation from fair pricing

#### A2: Structural Mispricing — Non-Exhaustive Markets (Research Phase, Lower Priority)

**Thesis:** In non-exhaustive neg-risk markets (no "Other" option), the sum of all YES prices is structurally < $1.00 because probability mass is allocated to unlisted outcomes. The corresponding NO tokens may be systematically overpriced because they price in the probability of ALL non-YES outcomes, including unlisted ones.

**Example:** Nobel Peace Prize market has 20 candidates listed, sum(YES) = $0.657. Each NO token prices at ~$0.95+, but the actual probability of any single candidate NOT winning includes the ~34% chance that an unlisted person wins. Is the NO price correctly accounting for this? If NO is priced at $0.95 but the true probability of that candidate losing is 97%, there's a 2% edge.

**Why this is separate from A1:**
- A1 is about **psychology** (people overpay for YES) — testable with simple win/loss backtest
- A2 is about **market structure** (how neg-risk pricing handles incomplete outcome sets) — requires understanding Polymarket's pricing math
- A2 is harder to backtest because the edge depends on understanding what NO tokens actually represent in non-exhaustive markets

**Status:** Research phase. Park this until A1 backtest is complete. If A1 shows weak results, investigate A2 as alternative.

---

### Strategy B: Leader-Follower (SECONDARY — After A1 Validation)

**Thesis:** When a "leader" market resolves, logically related "follower" markets experience a pricing delay of 10-120 seconds. During this window, the follower's price hasn't adjusted to reflect the new information, creating 5-20% edge opportunities.

**Examples:**
- "Fed cuts rates" resolves YES → "S&P 500 up this month" still at pre-announcement price
- "Team X wins semifinal" resolves YES → "Team X wins championship" hasn't adjusted upward
- "Candidate wins primary" resolves YES → "Candidate wins general election" still at old odds
- "Country A signs treaty" resolves → "Sanctions lifted on Country A" hasn't moved

**Why this is good for us:**
- Edge comes from understanding **logical relationships between events**, not speed
- Each trade has a clear thesis and bounded risk
- Higher edge than Type 1 (5-20% vs 0.5-4%)
- Market knowledge is a defensible advantage — hard to automate generically
- Works with settlement events that happen at known times (scheduled announcements, game results)

**Approach:**
1. Build a relationship graph of Polymarket markets (manual rules + text similarity)
2. Monitor market resolutions via WebSocket (real-time settlement feed)
3. When a leader resolves, immediately check follower prices against expected values
4. If follower price delta > min_edge: execute trade
5. Exit when follower price adjusts (or set take-profit limit order)

**Key technical requirements:**
- WebSocket connection for real-time resolution events
- Pre-built relationship mapping (manual rules for high-value categories, embedding similarity for discovery)
- Moderate speed needed (seconds, not milliseconds — the delay window is 10-120s)
- Must handle false relationships gracefully (leader resolves but follower doesn't actually need to move)

---

### Type 1: Multi-Option Rebalancing (LOW PRIORITY — Background)

**Status:** Scanner built, exhaustiveness filter added, runs in 5 seconds.

**Current reality:** ~8 viable events at any time, edges 0.5-4% after CLOB verification. Run as background check during main loop, don't build dedicated trading infrastructure.

**Requirements already implemented:**
- Exhaustive outcome filter (catch-all keyword detection)
- Gamma outcomePrices fast scan (zero CLOB API calls for filtering)
- Two-tier verification (Gamma filter → CLOB ask/bid for candidates only)
- min_profit raised to 2% to avoid wasting execution on tiny edges

---

## Updated Project Structure

```
polymarket-arb/
├── CLAUDE.md
├── polymarket-arb-blueprint-v2.md       ← THIS FILE
├── config/
│   ├── settings.py                      ← Updated risk config
│   └── relationships.py                ← For Strategy B leader-follower pairs
├── src/
│   ├── client.py                       ← Working (CLOB + Gamma, verified)
│   ├── notifier.py                     ← Discord webhooks (placeholder)
│   ├── scanner/
│   │   ├── base.py                     ← Base scanner class
│   │   ├── rebalance.py                ← Type 1 (working, low priority)
│   │   ├── logical.py                  ← Type 2 base (becomes Strategy B)
│   │   ├── no_bias.py                  ← NEW: Strategy A live scanner
│   │   └── leader_follower.py          ← NEW: Strategy B scanner
│   ├── backtester/                     ← NEW: historical analysis
│   │   ├── __init__.py
│   │   ├── data_loader.py              ← Fetch + cache resolved market history
│   │   ├── no_bias_backtest.py         ← Strategy A1 backtest engine
│   │   └── report.py                   ← Generate analysis reports
│   ├── evaluator.py                    ← Opportunity evaluation (placeholder)
│   ├── risk.py                         ← Risk manager (placeholder)
│   ├── trader/
│   │   ├── paper.py                    ← Paper trader (placeholder)
│   │   └── live.py                     ← Live trader (placeholder)
│   ├── models.py                       ← Working (verified against live API)
│   └── db.py                           ← Working (SQLite, includes rejected_opportunities)
├── dashboard/
│   └── terminal.py                     ← Rich dashboard (placeholder)
├── data/
│   ├── diagnostics/                    ← Existing diagnostic runs (gitignored)
│   ├── historical/                     ← NEW: resolved market data (gitignored)
│   └── backtests/                      ← NEW: backtest results (gitignored)
├── scripts/
│   ├── diagnose_api.py                 ← Working (fast/default/full modes)
│   ├── fetch_historical.py             ← NEW: download all resolved markets
│   ├── backtest_no_bias.py             ← NEW: run Strategy A1 backtest
│   ├── scan_once.py                    ← Working (Type 1 + Type 2)
│   └── export_trades.py                ← Export trade log (placeholder)
├── tests/
│   └── ...                             ← Test stubs (existing)
├── docs/
│   └── superpowers/                    ← Superpowers specs and plans
└── main.py                             ← Main loop (placeholder)
```

---

## Phase Plan (Updated)

### Phase 1: Strategy A1 Backtest (THIS WEEK)
**Goal:** Prove or disprove retail optimism bias with historical data

**Step 1: Data Exploration (diagnostic-first, like we did for active markets)**
1. Fetch 10 resolved markets from Gamma API, inspect raw response
2. Find the field that indicates which outcome won
3. Understand what tags/categories are available
4. Estimate total resolved market count

**Step 2: Data Collection**
1. Fetch ALL resolved markets (paginate through everything)
2. Parse and save to data/historical/

**Step 3: Backtest Analysis**
1. Classify each market by category (using tags, keywords)
2. For each resolved binary market:
   - Record final YES price (outcomePrices[0])
   - Record whether YES actually won
   - Calculate what buying NO would have returned
3. Group by category × YES price bucket
4. Generate statistical report

**Step 4: Decision**
- If positive signal found → Phase 2 (paper trading)
- If no signal → skip to Phase 3 (Strategy B)

**Duration:** 3-5 days

### Phase 2: Strategy A1 Paper Trading (IF backtest positive)
**Goal:** Validate live signals match backtest predictions

1. Build live scanner for winning categories
2. Discord alerts on new opportunities
3. Paper trade 2-4 weeks
4. Compare actual vs predicted win rate

**Duration:** 2-4 weeks

### Phase 3: Strategy B Development (PARALLEL or after Phase 2)
**Goal:** Build leader-follower settlement-triggered system

1. Explore WebSocket API for resolution events
2. Build relationship mapping
3. Build resolution monitor + follower price checker
4. Paper trade with Discord alerts

**Duration:** 2-3 weeks

### Phase 4: Live Trading ($100-200)
**Goal:** Deploy proven strategies

1. Strategy A1 (if validated) — most data-backed
2. Strategy B (after paper trading) — highest potential edge
3. Type 1 background scanner — bonus
4. Start $100-200, scale based on results

---

## Strategy A1: Detailed Implementation

### Data Collection Script

```python
# scripts/fetch_historical.py
#
# Standalone script (like diagnose_api.py — minimal project imports)
#
# 1. Fetch all resolved (closed) markets from Gamma API
#    API: GET https://gamma-api.polymarket.com/markets?closed=true&limit=500
#    Paginate through ALL pages (expect 10,000+ markets)
#
# 2. For each market, capture:
#    - id, question, slug, description
#    - Category info: tags (array), event title/category
#    - Pricing: outcomePrices (JSON string → [YES_price, NO_price])
#    - Outcome: outcomes (JSON string → ["Yes", "No"]),
#      CRITICAL: find the field that says WHO WON
#      Check: resolutionSource, resolved, winner, outcome,
#             outcomePrices at resolution time
#    - Volume: volumeNum (not volume24hr)
#    - Timing: createdAt, closedTime, endDate
#    - Market type: negRisk, clobTokenIds
#
# 3. Save raw JSON to data/historical/raw_resolved_markets.json
# 4. Save parsed summary to data/historical/resolved_summary.json
#
# KEY UNKNOWN: How does Gamma API indicate which outcome won?
# → First step: fetch 5 resolved markets and inspect ALL fields
#   to find the winner/resolution indicator.
#   This is the diagnostic-first approach that worked well for us.
#
# Rate limiting: same 60 req/min pattern as diagnose_api.py
# Expected: ~20 pages × 500 markets = 10,000 markets in ~20 API calls
```

### Backtest Engine

```python
# scripts/backtest_no_bias.py
#
# Reads data/historical/resolved_summary.json
#
# For each resolved BINARY market (YES/NO only):
#   1. Get final YES price from outcomePrices[0]
#   2. Determine if YES won (from resolution data)
#   3. Calculate NO buyer's P&L:
#      - NO cost = 1 - YES_price (approximate, actual = CLOB ask for NO)
#      - If YES won: NO buyer loses their stake
#      - If YES lost (NO won): NO buyer gets $1.00 payout
#      - P&L = payout - cost
#
# Group analysis:
#   a) By category (crypto, politics, sports, entertainment, etc.)
#   b) By YES price bucket:
#      - 0.50-0.60 (coin flip zone)
#      - 0.60-0.70 (moderate favorite)
#      - 0.70-0.80 (strong favorite)
#      - 0.80-0.90 (heavy favorite)
#      - 0.90-1.00 (near certain)
#   c) By volume tier (high >$100k, medium $10k-$100k, low <$10k)
#   d) By market age (how long was the market open)
#
# Output report (data/backtests/no_bias_report.json):
#   Per category:
#     - N markets, NO win rate, NO win rate by price bucket
#     - EV of buying NO at each price point
#     - Calibration: does 70% YES actually win 70% of the time?
#     - Statistical significance test (chi-squared or binomial)
#   Overall:
#     - Calibration curve across all markets
#     - Top categories by NO edge
#     - Recommended trading parameters (category, price range, Kelly fraction)
```

### Hypotheses Ranked by Expected Signal Strength

```
Priority 1 (strongest expected bias):
  - Crypto price targets ("Will BTC hit $X")
  - Airdrop/token launch values
  - FDV predictions
  → Retail crypto traders are structurally bullish

Priority 2 (moderate expected bias):
  - Celebrity/entertainment ("Will X win award")
  - Sports preseason predictions
  → Fan enthusiasm inflates YES

Priority 3 (weak expected bias, useful as control):
  - Political elections
  - Economic indicators
  → If these also show NO bias, something structural is happening

Priority 4 (separate mechanism — Strategy A2):
  - Non-exhaustive multi-outcome markets
  → Structural mispricing, not psychology
```

---

## Strategy B: Detailed Implementation

### Relationship Types

```python
LEADER_FOLLOWER_PAIRS = {
    # Sports: tournament progression
    "semifinal_to_final": {
        "leader_pattern": r"(wins?|advance|beat).*semi.?final",
        "follower_patterns": [
            r"wins?.*championship",
            r"wins?.*final",
            r"wins?.*title",
        ],
        "direction": "if_leader_yes_then_follower_up",
        "expected_edge": "10-30%",
    },
    # Politics: primary → general
    "primary_to_general": {
        "leader_pattern": r"wins?.*primary",
        "follower_patterns": [
            r"wins?.*general",
            r"elected.*president",
            r"wins?.*election",
        ],
        "direction": "if_leader_yes_then_follower_up",
        "expected_edge": "5-15%",
    },
    # Macro: policy → market impact
    "fed_rate_to_market": {
        "leader_pattern": r"Fed.*(cut|hike|hold).*rate",
        "follower_patterns": [
            r"S&P.*end of.*(month|quarter)",
            r"Treasury.*yield",
            r"recession.*20\d{2}",
        ],
        "direction": "conditional",
        "expected_edge": "5-10%",
    },
    # Geopolitics: agreement → consequence
    "treaty_to_consequence": {
        "leader_pattern": r"(sign|agree|peace|ceasefire|deal)",
        "follower_patterns": [
            r"sanctions.*lift",
            r"trade.*resume",
            r"troops.*withdraw",
        ],
        "direction": "if_leader_yes_then_follower_up",
        "expected_edge": "10-20%",
    },
}
```

### Resolution Monitoring Architecture

```
WebSocket Feed (real-time)
    │
    ▼
Resolution Detector
    │ "Market X just resolved YES"
    ▼
Relationship Lookup
    │ "Market X is LEADER for markets Y, Z"
    ▼
Follower Price Check (CLOB API)
    │ "Market Y: current $0.40, expected post-resolution $0.65"
    ▼
Edge Calculator
    │ "Edge: 25% → TRADE SIGNAL"
    ▼
Risk Manager → Trader → Discord Alert
```

---

## Updated Risk Config

```python
RISK_CONFIG = {
    # Position limits
    "max_single_trade_usd": 20,
    "max_position_per_market_usd": 50,
    "max_total_exposure_usd": 200,

    # Strategy A1: Systematic No Bias
    "strategy_a_min_win_rate": 0.55,
    "strategy_a_min_sample_size": 100,
    "strategy_a_yes_price_min": 0.60,
    "strategy_a_yes_price_max": 0.90,
    "strategy_a_min_volume": 5000,
    "strategy_a_kelly_fraction": 0.25,       # Quarter-Kelly

    # Strategy B: Leader-Follower
    "strategy_b_min_edge_pct": 5.0,
    "strategy_b_max_time_since_resolution": 120,
    "strategy_b_min_follower_volume": 10000,
    "strategy_b_min_confidence": 0.7,

    # Type 1 (low priority)
    "type1_min_profit_pct": 2.0,
    "type1_require_exhaustive": True,

    # Stop-loss (all strategies)
    "daily_loss_limit_usd": 20,
    "consecutive_loss_limit": 3,
    "max_drawdown_pct": 10,
    "max_trades_per_hour": 20,
    "cooldown_after_loss_minutes": 30,
}
```

---

## Claude Code Session Guide

### NEXT SESSION: Strategy A1 — Data Exploration

```
Read polymarket-arb-blueprint-v2.md first. Focus on "Strategy A1" 
and "Data Collection Script" sections.

/superpowers:brainstorm

Phase 1 of our strategy pivot: backtest whether buying NO on 
Polymarket has a systematic edge in certain categories.

FIRST TASK (diagnostic-first approach, like we did for active markets):
Fetch 10 resolved (closed) markets from the Gamma API and 
examine the raw response. I need to find:
1. Which field indicates which outcome WON (the resolution result)
2. What outcomePrices looks like for resolved markets
3. What tags/categories are available for classification
4. How many total resolved markets exist

API: GET https://gamma-api.polymarket.com/markets?closed=true&limit=10

Save raw response to data/historical/sample_resolved.json.
Report findings — especially the resolution/winner field.

DO NOT build the full backtest yet. Explore data first.
```

### LATER: Strategy A1 — Full Backtest

```
Read polymarket-arb-blueprint-v2.md.

Based on data exploration, build the full backtest:

/superpowers:brainstorm

Build scripts/fetch_historical.py and scripts/backtest_no_bias.py.
Key question: in which market categories does buying NO have 
a statistically significant edge?

Follow the same pattern as our diagnostic: 
fetch → analyze → report → decide.
```

### LATER: Strategy B — Leader-Follower

```
Read polymarket-arb-blueprint-v2.md. Focus on Strategy B.

/superpowers:brainstorm

Building leader-follower settlement-triggered trading.
Start by exploring:
1. Polymarket WebSocket API for market resolution events
2. How to detect when a market has just resolved in real-time
3. The Sports WebSocket endpoint in their docs

First task: connect to WebSocket and log resolution events 
to understand the data format. Don't build the full system yet.
```

---

## What's Already Working (Don't Rebuild)

| Component | Status | Notes |
|-----------|--------|-------|
| src/client.py | ✅ Working | Gamma + CLOB, rate limiting, all fixes applied |
| src/models.py | ✅ Working | Pydantic v2, verified against live API |
| src/db.py | ✅ Working | SQLite, includes rejected_opportunities table |
| src/scanner/rebalance.py | ✅ Working | Type 1 with exhaustiveness filter |
| src/scanner/logical.py | ✅ Working | Type 2 base (import/constructor fixed) |
| scripts/diagnose_api.py | ✅ Working | fast/default/full modes, Gamma optimization |
| scripts/scan_once.py | ✅ Working | Runs both scanners end-to-end |
| .claude/hooks/notify_discord.sh | ✅ Working | Discord notification on completion |
| config/settings.py | ✅ Working | RISK_CONFIG with updated thresholds |

Total existing infrastructure: ~2,000 lines, all verified against live Polymarket API.
