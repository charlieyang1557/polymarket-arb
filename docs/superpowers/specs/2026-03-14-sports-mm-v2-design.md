# Sports MM v2 — Volatility-Aware Quoting

## Context

Paper trading results on KXNCAAMBSPREAD-26MAR13OKLAARK-ARK6 (2026-03-13):
- **Both-sided fills confirmed**: 8 YES + 6 NO in 2.5 hours (vs 0 YES in 24h on political markets)
- **Queue time**: 11 seconds during live game (vs 3-17 hours political)
- **Round-trips completed**: +1.4c realized P&L before price swing
- **Adverse selection**: 9c midpoint move during game → -10c realized P&L
- **Bug found**: PAUSE_30MIN looped forever (fixed — `consecutive_losses` now resets)

**Conclusion**: Sports MM is structurally viable. The one-sided fill problem that killed political MM does not exist in sports spread/total markets. The remaining problem — adverse selection during in-game volatility — is solvable with mode-aware quoting.

## A) Pre-Game vs Live-Game Modes

### The Problem

Sports markets have two distinct regimes:
1. **Pre-game** (hours before tipoff): Stable prices, slow queue drain, deep books. Safe to quote aggressively.
2. **Live-game** (during play): Rapid price swings on scoring runs, timeouts, injuries. Current 2c spread gets run over by 5-10c moves.

### Detection: Trade Frequency Spike

```
Pre-game:  ~50-100 trades/hr, 1-5 contracts per trade
Live-game: 500-2000+ trades/hr, 50-500 contracts per trade
Transition: detectable within 2-3 ticks (20-30 seconds)
```

**Implementation**: Track rolling 5-minute trade count. If `trades_5min > 50` (equivalent to >600/hr), switch to live-game mode.

```python
# In MarketState, add:
trade_timestamps: list[datetime]  # rolling window of trade times

# Detection (in engine tick):
cutoff = now - timedelta(minutes=5)
recent = [t for t in ms.trade_timestamps if t > cutoff]
is_live_game = len(recent) > 50
```

### Mode Behavior

| Parameter | Pre-Game | Live-Game |
|-----------|----------|-----------|
| Quote spread | Best bid (aggressive) | Best bid - 2c (wider) |
| L4 price jump threshold | 5c / 60s | 2c / 60s |
| Max order size | 2 contracts | 1 contract |
| Requote threshold | 2c from best bid | 1c from best bid |
| Pause on fill | None | 30s cooldown after fill |

The 30s post-fill cooldown in live-game mode is the key defense: after getting filled, the market is likely moving. Don't immediately requote — wait for the new equilibrium.

### Mode Transition

- **Pre → Live**: Immediate switch on detection. Cancel and requote at wider spread.
- **Live → Pre**: Require 5 minutes of `trades_5min < 20` before switching back. Don't oscillate.
- **Game end**: When market resolves or trade frequency drops to near-zero for 10+ minutes.

## B) Adverse Selection Protection

### The Problem We Observed

```
T=0:  mid=48c, our YES bid at 47c, queue_pos=8410
T=10min: mid=46c, requoted YES bid at 46c (followed market down)
T=12min: mid=39c (!), got filled at 39c — we bought YES at 39c
         but only because the market crashed THROUGH our price
         → we're now holding YES at 39c in a falling market
```

This is textbook adverse selection: we get filled precisely when we don't want to be (the market moved against us).

### Tighter Price Jump Detection

Current L4 threshold: 5c move in 60 seconds → PAUSE_60S.
The Arkansas game had a 9c move, but it happened in chunks:
- 48→46 (2c in 5min — within threshold)
- 46→39 (7c in 2min — triggers L4 but too late, already filled at stale price)

**New thresholds (live-game mode only)**:

| Window | Threshold | Action |
|--------|-----------|--------|
| 60s | 3c move | Cancel all, wait 30s |
| 30s | 2c move | Cancel all, wait 15s |

This is much tighter than the current 5c/60s. It will occasionally pause quoting during normal volatility — that's acceptable. Missing a few fills is cheaper than adverse selection.

### Implementation

```python
def check_adverse_selection(ms: MarketState, is_live_game: bool) -> Action:
    """Tighter price jump check during live games."""
    if not is_live_game or len(ms.midpoint_history) < 2:
        return Action.CONTINUE

    now = ms.midpoint_history[-1][0]
    current_mid = ms.midpoint_history[-1][1]

    for ts, mid in reversed(ms.midpoint_history):
        age = (now - ts).total_seconds()
        if age <= 30 and abs(current_mid - mid) > 2:
            return Action.PAUSE_60S  # reuse existing pause mechanism
        if age <= 60 and abs(current_mid - mid) > 3:
            return Action.PAUSE_60S

    return Action.CONTINUE
```

### Post-Fill Cooldown

After any fill in live-game mode, set `ms.paused_until = now + 30s`. This prevents the scenario where we get filled, immediately requote, and get adversely selected again on the next wave.

## C) Daily Auto-Discovery Pipeline

### The Problem

Sports markets rotate daily. Yesterday's NCAA games are settled, tonight's are new tickers. Manual market selection doesn't scale.

### Pipeline: `scripts/kalshi_daily_scan.py`

Run daily at **9:00 AM ET** (markets listed by then, games start 6-11pm ET).

```
1. Fetch all events with nested markets (reuse kalshi_mm_scanner.py logic)
2. Filter to today's sports events:
   - expected_expiration_time is today
   - category in (Sports)
   - ticker contains SPREAD or TOTAL (not game outcomes — those are asymmetric)
3. For each candidate, fetch orderbook:
   - spread >= 3c
   - symmetry ratio 0.3-3.0
   - total book depth > 1000 contracts per side
4. Estimate expected volume from similar past markets (or use 50K minimum)
5. Output: JSON list of tickers, sorted by MM score
6. Feed into paper_mm.py:
   python scripts/paper_mm.py --tickers $(cat data/daily_targets.txt)
```

### Schedule

```
09:00 ET  kalshi_daily_scan.py → data/daily_targets.json
09:05 ET  paper_mm.py starts with today's targets (pre-game mode)
~6-11pm   Games start (auto-detects live-game mode)
~9pm-1am  Games end (markets resolve, bot exits cleanly)
```

### Cron Job (future)

```bash
# Daily market scan + paper MM launch
0 14 * * * cd /path/to/polymarket-arb && python scripts/kalshi_daily_scan.py
5 14 * * * cd /path/to/polymarket-arb && python scripts/paper_mm.py \
    --tickers "$(python -c "import json; print(','.join(t['ticker'] for t in json.load(open('data/daily_targets.json'))[:5]))")" \
    --duration 43200
```

### Market Type Priority

From our scan (2026-03-13), ranked by MM suitability:

1. **NCAA basketball spread** — best symmetry (0.8-1.0), highest volume, 3-5c spreads
2. **NCAA basketball total** — similar to spread, slightly lower frequency
3. **NHL game outcomes** — decent symmetry (0.95), good volume
4. **ATP/WTA tennis** — moderate volume, good spreads, shorter games
5. **MLB spring training** — lower volume but still symmetric

Avoid: game outcome markets (win/lose) — they have the same asymmetry problem as political markets.

## Implementation Priority

1. **PAUSE_30MIN bug fix** — Done (this session)
2. **Live-game detection** — Add `trade_timestamps` tracking, frequency-based mode switch
3. **Tighter L4 thresholds** — Parameterize by mode (pre-game vs live-game)
4. **Post-fill cooldown** — 30s pause after fill in live-game mode
5. **Daily auto-discovery** — Extract scanner logic into reusable function
6. **Paper test v2** — Run on 2-3 tonight's games with these fixes

Steps 2-4 are ~100 lines of code changes. Step 5 is a thin wrapper around `kalshi_mm_scanner.py`.
