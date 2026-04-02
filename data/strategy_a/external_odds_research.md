# External Odds Data Source Evaluation
**Date:** 2026-04-01 | **Branch:** feature/strategy-a

## Context
Phase 1 internal calibration showed ~2.5% gross bias in longshot buckets,
which doesn't survive taker fees. Jumping to strongest alpha factor:
external sportsbook odds (Pinnacle closing lines).

---

## 1. The-Odds-API

**URL:** https://the-odds-api.com

### Free Tier (500 credits/month)
- Live/upcoming odds ONLY — **no historical data**
- Historical requires $30/month minimum (20K credits)
- Historical endpoint costs 10x normal (10 credits/request)
- So $30/month = ~2,000 historical snapshots

### Coverage
- All target sports: NBA, NFL, NHL, MLB, NCAA, UFC, Tennis
- **Pinnacle included** (EU region bookmaker)
- Also includes: DraftKings, FanDuel, Betfair, 40+ bookmakers
- Also lists **Kalshi and Polymarket** as bookmakers (!)
- Markets: moneyline, spreads, totals, player props (select sports)
- Historical data back to June 2020, 5-minute intervals from Sept 2022

### Data Format
- JSON, decimal or American odds via `oddsFormat` parameter
- ISO 8601 timestamps
- Nested structure: event → bookmaker → market → outcome → price

### Verdict
- **Best option for live forward-collection** (free tier, 500 req/month)
- Historical requires $30/month — worth it if we commit to Strategy A
- The fact they list Polymarket as a bookmaker is interesting for cross-referencing

---

## 2. Free Alternatives for Historical Data

### OddsPortal Scrapers (Free, DIY)
- OddsPortal.com has massive historical database including Pinnacle
- Open-source scrapers: OddsHarvester (Python), odds-portal-scraper
- **Covers all sports, includes Pinnacle closing lines**
- Risk: scraping, may break, TOS issues
- **Best free option for historical Pinnacle data**

### Kaggle Datasets (Free)
- "Beat The Bookie" — 500K+ football/soccer matches, 11 years, **includes Pinnacle**
- NBA odds datasets exist but generally lack Pinnacle-specific lines
- Static snapshots, may be stale (2018-2020 vintage)

### OddsPapi (Free tier: 250 req/month)
- Claims Pinnacle historical data with NO 10x multiplier
- Worth investigating as cheaper alternative to The-Odds-API

### Odds Warehouse ($79 one-time)
- CSV downloads: MLB, NBA, NFL, NHL, Soccer, Tennis
- 10+ years of data for $79
- Unclear which bookmakers — may not have Pinnacle specifically

---

## 3. Polymarket US Resolved Market Count

### Current State (2026-04-01)
- **Total closed markets: 56**
- **TERMINATED (actually resolved): 3**
- **PENDING (closed, not settled): 53**
- **Currently active: 100** (all futures — no daily games today)

### Throughput Estimate
- Platform is very new — only 56 closed markets total
- All 100 active markets are "futures" type (season-long bets)
- Daily game markets appear and disappear same-day
- Estimated ~20-50 markets per day during active sports seasons
- **At 30 markets/day: 200 data points in ~7 days**
- **At 50 markets/day: 200 data points in ~4 days**

### Matching Challenge
- Only 3 markets have actually settled (TERMINATED)
- 53 are closed but pending settlement
- Forward-collection is the only viable path for Polymarket US data
- Cannot do historical backtest on US data — it doesn't exist yet

---

## 4. Forward Collection Strategy

### Minimum Viable Data Collection
1. **Free tier The-Odds-API** (500 req/month):
   - Fetch Pinnacle odds for tonight's NBA/NHL/MLB games: ~10-20 req/day
   - Budget: 500 / 30 days = ~16 requests/day — tight but workable
   - Focus on moneylines + spreads only (most liquid on Polymarket)

2. **Polymarket US SDK** (unlimited, public):
   - Fetch BBO for matching markets: ~10-20 req/day
   - Already have this infrastructure in poly_daily_scan.py

3. **Daily log format:**
   ```json
   {
     "date": "2026-04-01",
     "event": "NBA: Lakers vs Celtics",
     "pinnacle_ml_home": 1.85,
     "pinnacle_ml_away": 2.05,
     "pinnacle_devigged_home": 0.525,
     "polymarket_yes_price": 0.53,
     "polymarket_slug": "aec-nba-lal-bos-2026-04-01",
     "delta": -0.005,
     "outcome": null  // filled after settlement
   }
   ```

4. **Timeline:**
   - Week 1-2: Collect 100-200 matched data points
   - Week 2-3: First analysis — is there a delta pattern?
   - Week 3-4: If delta > 3% consistently, start paper trading signals

### Data Points Needed
- 200 minimum for directional signal (is delta positive or negative?)
- 500+ for per-sport breakdown
- 1000+ for reliable Brier Score comparison
- At ~30-50/day during NBA/MLB season, 200 points = 4-7 days

---

## 5. Recommendation

### Immediate (Free, start today)
1. Sign up for The-Odds-API free tier (500 req/month)
2. Build daily collector script: fetch Pinnacle + Polymarket for same events
3. De-vig Pinnacle lines using power method (simpler than Shin's for 2-way)
4. Log delta to SQLite daily

### If signal found (Week 2-3)
5. Upgrade to $30/month for historical backtest validation
6. OR scrape OddsPortal for historical Pinnacle closing lines (free)

### Kill condition
- If after 200+ matched data points, mean |delta| < 2%:
  Pinnacle and Polymarket are too correlated for profitable trading
- If delta exists but wrong direction (Polymarket more accurate):
  Our alpha thesis is wrong

---

## 6. Key Insight

Polymarket US is too new for historical backtest. The only path forward
is forward-collection. This is actually fine — if we can prove edge in
2-3 weeks of live data, that's more convincing than any historical
backtest on a different platform (Polymarket Global).
