# Trade Tape Collector — Operational Setup

**Generated:** 2026-05-11.
**Script:** [scripts/trade_tape_collector.py](../../scripts/trade_tape_collector.py)
**Tests:** [tests/test_trade_tape_collector.py](../../tests/test_trade_tape_collector.py) (23 passing)

## Purpose

Subscribe to Polymarket US WebSocket `SUBSCRIPTION_TYPE_TRADE` for all
currently-active bot markets and persist every trade — including the
maker and taker side+intent — to `data/poly_trade_tape.db`. This data
unlocks:

1. **Aggressor-aware `drain_queue()`**: fix the over-counting documented
   in [memory/paper_vs_live_gap.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/paper_vs_live_gap.md).
2. **Round-trip simulator v2**: replay live fills with real queue
   dynamics instead of last-snapshot BBO approximation.
3. **VPIN toxicity gate**: per Bartlett & O'Hara Section 6.

## Smoke-test result (2026-05-11 13:39 PDT, 90s window)

```
subscribed +7 slugs (now 7)
shutdown. final stats: {'trades_received': 3, 'trades_persisted': 3, 'errors': 0}
```

Sample row (confirms real SDK enum format — NOT "yes"/"no"/"buy"/"sell"):

```
slug:          asc-nba-okc-lal-2026-05-11-neg-12pt5
price_cents:   48
quantity:      138 contracts
maker_side:    ORDER_SIDE_BUY
maker_intent:  ORDER_INTENT_UNDEFINED
taker_side:    ORDER_SIDE_SELL
taker_intent:  ORDER_INTENT_BUY_SHORT
trade_time:    2026-05-11T20:40:18.456Z
```

Interpretation: a taker SOLD YES at 48c (`taker.intent=BUY_SHORT` means
the taker was establishing a short YES = long NO position; their
mechanic was to sell YES). This **drained the YES_BID side** of the
orderbook. For our YES-bid maker at this price, this is the kind of
trade that hits us.

## Schema

```sql
CREATE TABLE mm_trade_tape (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug TEXT NOT NULL,
    price_cents INTEGER NOT NULL,                       -- 0-100
    quantity INTEGER NOT NULL,                           -- contract count
    trade_time TEXT NOT NULL,                            -- ISO-8601 UTC
    maker_side TEXT NOT NULL,                            -- ORDER_SIDE_BUY/SELL
    maker_intent TEXT NOT NULL,                          -- ORDER_INTENT_*
    taker_side TEXT NOT NULL,
    taker_intent TEXT NOT NULL,
    raw_json TEXT,                                       -- full message
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX idx_trade_tape_slug_time ON mm_trade_tape(market_slug, trade_time);
```

## Polymarket enum decode

Real-data observed enums (more may exist):

| Field | Values |
|---|---|
| `side` | `ORDER_SIDE_BUY`, `ORDER_SIDE_SELL` |
| `intent` | `ORDER_INTENT_BUY_LONG` (buy YES), `ORDER_INTENT_BUY_SHORT` (buy NO via sell YES), `ORDER_INTENT_SELL_LONG` (sell YES closing long), `ORDER_INTENT_SELL_SHORT` (sell NO closing short), `ORDER_INTENT_UNDEFINED` (often on maker side) |

For drain-analysis: the orderbook side hit is determined by the
combination `(taker.side, taker.intent)`. The helper functions
`taker_yes_or_no()` and `taker_buy_or_sell()` in
[trade_tape_collector.py](../../scripts/trade_tape_collector.py) provide
first-pass classification; the analysis layer (drain-queue fix and
round-trip simulator v2) can refine.

## Running

Foreground (for testing or operator-attended runs):

```bash
cd /Users/openclaw/polymarket-arb
python scripts/trade_tape_collector.py
```

With a duration cap (for smoke tests):

```bash
python scripts/trade_tape_collector.py --duration 300 --verbose
```

Custom paths:

```bash
python scripts/trade_tape_collector.py \
    --db /Users/openclaw/polymarket-arb/data/poly_trade_tape.db \
    --slugs /Users/openclaw/polymarket-arb/data/poly_active_slugs.json \
    --refresh-interval 10
```

## Recommended daemon setup (pm2)

Per [memory/log_file_paths.md](file:///Users/openclaw/.claude/projects/-Users-openclaw-polymarket-arb/memory/log_file_paths.md),
the live bot uses pm2. Add the trade tape collector as a sibling
process:

```bash
# from /Users/openclaw/polymarket-arb
pm2 start scripts/trade_tape_collector.py \
    --name trade-tape \
    --interpreter /Users/openclaw/miniconda3/bin/python \
    --output logs/trade-tape-out.log \
    --error logs/trade-tape-err.log
pm2 save
```

Or a one-shot ecosystem file (saved as `ecosystem.config.cjs`):

```javascript
module.exports = {
  apps: [{
    name: 'trade-tape',
    script: 'scripts/trade_tape_collector.py',
    interpreter: '/Users/openclaw/miniconda3/bin/python',
    cwd: '/Users/openclaw/polymarket-arb',
    autorestart: true,
    max_restarts: 50,
    restart_delay: 5000,
    out_file: 'logs/trade-tape-out.log',
    error_file: 'logs/trade-tape-err.log',
  }],
};
```

Then `pm2 start ecosystem.config.cjs`.

## Operational notes

- **Real-time only**: the WS does NOT support backfilling. Trades that
  happen while the collector is down are lost. Restart promptly on
  crash; the collector has internal exponential-backoff reconnect
  (max 60s).
- **No interference with the bot**: the collector connects to the WS
  independently of the bot's HTTP polling. They share no resources
  except the active-slugs JSON file (read-only from the collector).
- **DB writes are commit-per-trade**: high-frequency markets may produce
  bursts. Current schema has no batching — if write throughput becomes
  a bottleneck, batch via `executemany` with periodic commit.
- **Subscription refresh**: every 15s by default. Hot-adds from the
  scanner are picked up within one refresh cycle.

## Analysis queries (for when data accumulates)

```sql
-- Trade volume by aggressor direction per slug
SELECT
    market_slug,
    taker_side,
    taker_intent,
    COUNT(*) AS n_trades,
    SUM(quantity) AS total_qty
FROM mm_trade_tape
GROUP BY market_slug, taker_side, taker_intent
ORDER BY market_slug, n_trades DESC;

-- Yes-seller takers (the case the YES penalty was designed to address)
SELECT
    market_slug,
    COUNT(*) AS yes_seller_trades,
    SUM(quantity) AS yes_seller_qty,
    AVG(price_cents) AS avg_price
FROM mm_trade_tape
WHERE taker_side = 'ORDER_SIDE_SELL'
  AND taker_intent IN ('ORDER_INTENT_BUY_SHORT', 'ORDER_INTENT_SELL_LONG')
GROUP BY market_slug
ORDER BY yes_seller_qty DESC;

-- Cross-reference with our maker fills (live DB) to compute true
-- aggressor-aware fill rate
ATTACH '/Users/openclaw/polymarket-arb/data/poly_mm_live.db' AS live;
SELECT
    f.ticker,
    f.side,
    f.price,
    COUNT(t.id) AS matching_trades
FROM live.mm_fills f
LEFT JOIN mm_trade_tape t
  ON t.market_slug = f.ticker
 AND t.price_cents = f.price
 AND ABS(strftime('%s', t.trade_time) - strftime('%s', f.filled_at)) < 30
WHERE f.side IN ('yes_bid', 'no_bid')
GROUP BY f.id;
```

## Next steps once data accumulates

1. After 1-2 weeks of collection: rerun the round-trip simulator with
   aggressor-aware queue dynamics.
2. Validate the survival probability model from
   [yes_penalty_counterfactual.md](yes_penalty_counterfactual.md):
   were the "at BBO" yes_bid fills really hit by YES-seller takers
   65% of the time as the static counterfactual assumed?
3. Build a VPIN toxicity meter and gate quoting on it (Bartlett &
   O'Hara Section 6).
