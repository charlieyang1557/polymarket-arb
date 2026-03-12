# Kalshi Paper Trading Market Maker — Design Spec

## Overview

A paper trading market maker for Kalshi that simulates passive market making across 5 medium-liquidity political markets. The goal is to validate mechanics, measure fill rates, track inventory risk, and calculate realistic P&L before committing real capital.

**This is infrastructure and learning, not alpha generation.** Expected edge is 8-17 cents/hour across all markets — the value is in the data and validated tooling.

## Target Markets (Tier 1 from diagnostic)

| Ticker | Market | Spread | Net Edge | Vol/hr |
|--------|--------|--------|----------|--------|
| KXGREENLAND-29 | Will Trump buy Greenland? | 5c | 4.33c | 95 |
| KXTRUMPREMOVE | Impeach and removed? | 3c | 2.42c | 47 |
| KXGREENLANDPRICE-29JAN21-NOACQ | No acquisition of Greenland | 2c | 1.38c | 110 |
| KXVPRESNOMR-28-MR | Rubio VP nominee | 2c | 1.31c | 100 |
| KXINSURRECTION-29-27 | Insurrection Act | 2c | 1.28c | 52 |

Selected criteria: spread >= 2c, positive edge after maker fees, queue depth at best bid < 1 hour of trade volume, daily volume > $10k equivalent.

## Architecture

```
scripts/paper_mm.py (entry point, CLI, main loop)
    |
    v
src/mm/engine.py (tick logic, fill simulation, quote management)
    |
    +-- KalshiClient.get_orderbook()  --+
    +-- KalshiClient.get_trades()     --+  10s poll cycle per market
    |                                    |
    v                                    |
src/mm/risk.py (Layers 1-4 checks)      |  Per tick:
    |                                    |  1. Fetch book + trades
    v                                    |  2. Drain queue from trade feed
src/mm/state.py (dataclasses)            |  3. Detect fills (queue_pos <= 0)
    |                                    |  4. Pair YES+NO -> realized P&L
    v                                    |  5. Risk checks (all layers)
src/db.py (4 new tables)  <-------------+  6. Place/cancel simulated orders
                                            7. Snapshot to DB every 60s
```

### File Plan

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `scripts/paper_mm.py` | ~50 | Entry point, CLI args, main loop |
| `src/mm/__init__.py` | 0 | Package marker |
| `src/mm/engine.py` | ~180 | Tick logic, fill simulation, quote management |
| `src/mm/risk.py` | ~80 | Layers 1-4, action priority |
| `src/mm/state.py` | ~70 | MarketState, SimOrder, GlobalState dataclasses |
| `src/db.py` | +40 | Add 4 tables to existing schema |
| **Total** | ~420 | |

### Main Loop (synchronous, no asyncio)

```python
# Staggered ticking: 5 markets, 10s interval per market
# T=0s  tick market 0
# T=2s  tick market 1
# T=4s  tick market 2
# T=6s  tick market 3
# T=8s  tick market 4
# T=10s tick market 0 (repeat)

while elapsed < duration:
    for i, market in enumerate(markets):
        if not market.active:
            continue
        if not is_my_tick(cycle, i, len(markets)):
            continue
        tick_one_market(market)
    sleep(interval / len(markets))
```

API budget: 5 markets x 2 calls/tick x 6 ticks/min = 60 calls/min. Kalshi allows 1200 reads/min (Basic tier) = 5% utilization.

## Fill Simulation

### FIFO Queue Position Model

When we "place" a simulated order at price P:
1. Record `queue_pos = depth_at_P` from current orderbook (we are at the back)
2. Each tick, fetch trades since `last_seen_trade_id`
3. Count trade volume at price <= P (for YES bids) or <= P (for NO bids)
4. Subtract from `queue_pos`
5. When `queue_pos <= 0`, our contracts are "filled" (capped at order size)

```
Time T0: Place YES bid at 26c, book depth at 26c = 42
         queue_pos = 42

Time T1: Trade feed shows 15 contracts traded at <= 26c
         queue_pos = 42 - 15 = 27

Time T2: Trade feed shows 30 contracts traded at <= 26c
         queue_pos = 27 - 30 = -3
         -> Fill 2 contracts (our order size), partial fill if size > drain
```

### Trade Deduplication

Kalshi trades have unique `trade_id`. We store `last_seen_trade_id` per market and only process new trades each tick. Trades with timestamps before our order placement are ignored.

### Partial Fills

If queue drains past our position but not by enough to fill all contracts:
- queue_pos = 2, our size = 5, drain = 4
- Fill 2 contracts (drain that passed through us), 3 remain resting

### Requoting

Cancel and requote when:
- Our order is > 2c away from current best bid (market moved away)
- Spread collapsed to 0 (crossed book = stale data, skip tick)
- Risk layer demands it (inventory breach, etc.)

### Aggress Fills

When inventory triggers aggress (Layer 2), simulate immediate taker fill at current ask price. No queue — taker orders fill instantly. Taker fee applied.

### Metrics Tracked Per Order

- `time_in_queue_s`: seconds from placement to fill (critical for capital efficiency)
- `queue_pos_initial`: starting queue position (for fill rate analysis)

## Data Model

### SimOrder

```python
@dataclass
class SimOrder:
    side: str           # "yes" or "no"
    price: int          # cents
    size: int           # contracts
    remaining: int      # unfilled contracts
    queue_pos: int      # contracts ahead of us
    placed_at: datetime
    last_drain_trade_id: str
```

### MarketState

```python
@dataclass
class MarketState:
    ticker: str
    active: bool
    yes_order: SimOrder | None
    no_order: SimOrder | None
    yes_queue: list[int]      # cost basis FIFO (filled YES prices)
    no_queue: list[int]       # cost basis FIFO (filled NO prices)
    realized_pnl: float       # cents, after fees and pair settlement
    unrealized_pnl: float     # cents, inventory marked to midpoint
    total_fees: float         # cents
    last_seen_trade_id: str
    consecutive_losses: int
    cumulative_pnl: float     # for Layer 3 per-market check
    paused_until: datetime | None  # for PAUSE_30MIN / PAUSE_60S
```

### GlobalState

```python
@dataclass
class GlobalState:
    markets: dict[str, MarketState]
    daily_pnl: float          # cents, sum across all markets
    peak_total_pnl: float     # for drawdown calculation
    total_pnl: float
    start_time: datetime
    db_error_count: int       # consecutive DB write failures
```

### Unrealized P&L Calculation

```python
def unrealized_pnl(state: MarketState, midpoint: float) -> float:
    if len(state.yes_queue) > len(state.no_queue):
        unhedged = state.yes_queue[len(state.no_queue):]
        return sum(midpoint - cost for cost in unhedged)
    elif len(state.no_queue) > len(state.yes_queue):
        unhedged = state.no_queue[len(state.yes_queue):]
        return sum((100 - midpoint) - cost for cost in unhedged)
    return 0.0
```

## Risk Management (5 Layers)

### Layer 1: Per-Order Validation

Checked before placing any simulated order.

- **Max size:** 5 contracts per order (paper trading)
- **Fat finger:** Order price must be within midpoint +/-10%
- **Hedge requirement:** Every order must have a corresponding order on the other side. Exception: aggress orders to flatten inventory.

### Layer 2: Inventory Management

Checked after every fill. Per-market.

| Net Position | Action |
|-------------|--------|
| <= 10 | CONTINUE |
| 11-20 | AGGRESS_FLATTEN (cross spread to reduce) |
| > 20 | STOP_AND_FLATTEN (cancel all resting, only flatten) |

**Time-based force close:** If oldest unhedged position is > 2 hours old, FORCE_CLOSE at market price regardless of P&L.

### Layer 3: P&L Circuit Breakers

Checked after every fill and every snapshot.

- **Daily loss:** > $5 (500c) across all markets -> FULL_STOP
- **Consecutive losses:** 3 round-trips with negative P&L in a row -> PAUSE_30MIN (per-market)
- **Per-market cumulative:** < -$10 (-1000c) -> EXIT_MARKET
- **Total drawdown:** Only triggers when peak > $1 (100c) AND drawdown > 50c AND drawdown > 5% of peak -> FULL_STOP

The drawdown triple-gate prevents false triggers on small absolute amounts.

### Layer 4: System Risk

Checked every tick. Per-market.

- **API disconnect:** > 30s since last successful response -> CANCEL_ALL (for that market)
- **Price jump:** Midpoint moved > 5c in last 60s (6 ticks) -> PAUSE_60S (per-market)
- **Crossed book:** Spread <= 0 -> SKIP_TICK
- **DB write failures:** 10 consecutive failures -> FULL_STOP (disk full or similar)

PAUSE_30MIN and PAUSE_60S are per-market. A price spike in Greenland does not pause quoting on House Control.

### Layer 5: Scaling Rules (Human Decision Gate)

Not enforced in code. After 48h paper run, human reviews:
- Paper profitable > 48h continuous -> allow $50 live
- $50 live profitable > 1 week -> allow $200
- $200 live profitable > 2 weeks -> allow $500-1000
- Any Layer 3 stop triggered -> regress to previous stage

### Action Priority (highest wins)

```
FULL_STOP > EXIT_MARKET > CANCEL_ALL > STOP_AND_FLATTEN >
FORCE_CLOSE > AGGRESS_FLATTEN > PAUSE_60S > PAUSE_30MIN >
SKIP_TICK > CONTINUE
```

Engine collects actions from all layers, takes highest priority, executes it. Every non-CONTINUE action is logged to mm_events.

### Default Behavior

On ANY error or unexpected state: STOP and CANCEL ALL. The bot should be harder to lose money with than to make money with.

## Database Schema

Four new tables in the existing SQLite database.

### mm_orders

```sql
CREATE TABLE mm_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    size INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    queue_pos_initial INTEGER,
    status TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    filled_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    time_in_queue_s REAL
);
```

### mm_fills

```sql
CREATE TABLE mm_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES mm_orders(id),
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    size INTEGER NOT NULL,
    fee REAL NOT NULL,
    is_taker INTEGER NOT NULL,
    inventory_after INTEGER,
    pair_id INTEGER,
    pair_pnl REAL,
    filled_at TEXT NOT NULL
);
```

### mm_snapshots

```sql
CREATE TABLE mm_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    best_yes_bid INTEGER,
    yes_ask INTEGER,
    spread INTEGER,
    midpoint REAL,
    net_inventory INTEGER,
    yes_held INTEGER,
    no_held INTEGER,
    realized_pnl REAL,
    unrealized_pnl REAL,
    total_pnl REAL,
    total_fees REAL,
    yes_order_price INTEGER,
    yes_queue_pos INTEGER,
    no_order_price INTEGER,
    no_queue_pos INTEGER,
    trade_volume_1min INTEGER,
    global_realized_pnl REAL,
    global_unrealized_pnl REAL
);
```

### mm_events

```sql
CREATE TABLE mm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT,
    layer INTEGER NOT NULL,
    action TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    net_inventory INTEGER,
    realized_pnl REAL,
    unrealized_pnl REAL,
    midpoint REAL,
    spread INTEGER,
    consecutive_losses INTEGER
);
```

### Key Queries

```sql
-- Fill rate per market
SELECT ticker, COUNT(*) as fills,
       AVG(time_in_queue_s)/3600 as avg_queue_hours
FROM mm_orders WHERE status IN ('filled','partial')
GROUP BY ticker;

-- Inventory exposure over time
SELECT ts, ticker, net_inventory, unrealized_pnl
FROM mm_snapshots ORDER BY ts;

-- Price spike frequency (>5c moves)
SELECT * FROM mm_events
WHERE layer = 4 AND action = 'PAUSE_60S';

-- Realistic daily P&L
SELECT date(ts) as day, ticker,
       MAX(realized_pnl) as realized,
       AVG(unrealized_pnl) as avg_unrealized
FROM mm_snapshots GROUP BY day, ticker;

-- Risk trigger frequency
SELECT layer, action, COUNT(*),
       AVG(net_inventory), AVG(realized_pnl)
FROM mm_events GROUP BY layer, action;

-- Fill probability per market (queue drain rate)
SELECT ticker, AVG(trade_volume_1min) as avg_vol_1min,
       AVG(yes_queue_pos) as avg_queue
FROM mm_snapshots WHERE yes_order_price IS NOT NULL
GROUP BY ticker;
```

## Error Handling

### API Errors

| Type | Examples | Action |
|------|----------|--------|
| Transient | 429, 500/502/503, timeout, connection reset | Retry with exponential backoff (1s, 2s, 4s), max 3 attempts |
| Fatal | 401 auth, 404 market gone, 403 forbidden | EXIT_MARKET |

After 3 consecutive transient failures: skip tick, log Layer 4 event.
After 30s of consecutive failures: CANCEL_ALL for that market.

### Tick-Level Isolation

Each market's tick is wrapped independently. An API error on one market does not affect the other 4.

### DB Write Failures

- Paper mode: log to stderr, continue. Counter tracks consecutive failures.
- 10 consecutive DB write failures: FULL_STOP (disk full or similar critical issue).
- Live mode (future): DB failure triggers CANCEL_ALL — cannot trade without audit trail.

### Graceful Shutdown

On SIGINT/SIGTERM/KeyboardInterrupt:
1. Cancel all simulated resting orders (log with cancel_reason="shutdown")
2. Write final snapshot for each market
3. Print session summary to terminal
4. Future live mode: cancel real orders via API before exit

## Terminal Output

```
Paper MM | 5 markets | 2 contracts | 10s interval
Started: 2026-03-12T00:30:00Z | Duration: 48h | DB: data/mm_paper.db
------------------------------------------------------------------------
[00:30:02] GREENLAND    mid=28c sprd=5 q_yes=42 q_no=38 inv=0 pnl=0.0c
[00:30:04] TRUMPREMOVE  mid=22c sprd=3 q_yes=25 q_no=19 inv=0 pnl=0.0c
[00:30:12] GREENLAND    mid=28c sprd=5 q_yes=27 q_no=38 inv=0 pnl=0.0c
[00:30:14] TRUMPREMOVE  >>> FILL [MAKER] yes_bid 2@21c fee=0.29c inv=+2 pnl=-0.3c
[00:31:02] GREENLAND    !!! RISK [L4] PAUSE_60S: midpoint moved 6c in 60s
```

Normal ticks: single-line status. Fills and risk events: highlighted full lines.

## Discord Notifications

Fire on:
- All fills (maker and taker)
- Layer 2+ risk events
- Session start and end summary

No notifications for normal ticks.

## CLI Interface

```
python scripts/paper_mm.py                            # all 5 Tier 1 markets, 48h
python scripts/paper_mm.py --tickers KXGREENLAND-29   # single market
python scripts/paper_mm.py --duration 3600            # 1 hour test
python scripts/paper_mm.py --size 3 --interval 15     # 3 contracts, 15s ticks
python scripts/paper_mm.py --db-path data/test.db     # custom DB location
```

## Fee Model

Kalshi quadratic fee structure:

- **Maker:** `0.0175 * contracts * P * (1-P)` dollars per fill
- **Taker:** `0.07 * contracts * P * (1-P)` dollars per fill

Where P = price in dollars (0 to 1). Maximum fee at P=0.50, near-zero at extremes.

Converted to cents for internal tracking:
- `maker_fee_cents = 0.0175 * count * (price/100) * (1 - price/100) * 100`
- `taker_fee_cents = 0.07 * count * (price/100) * (1 - price/100) * 100`

## Success Criteria for 48h Paper Run

After the run, query the database to answer:

1. **Fill rate:** How many fills per hour per market? How does actual compare to our diagnostic estimates?
2. **Queue time:** Average time_in_queue_s per market. How long is capital locked?
3. **Inventory exposure:** Max net_inventory and max unrealized loss observed.
4. **Event risk:** How many PAUSE_60S events (>5c moves)? How many affected our inventory?
5. **Realistic P&L:** Net realized_pnl after all fees. Is it positive?
6. **Risk trigger frequency:** How often did each layer fire? Any FULL_STOP events?

Decision gate: if cumulative P&L is positive and no FULL_STOP events, consider $50 live deployment per Layer 5 rules.
