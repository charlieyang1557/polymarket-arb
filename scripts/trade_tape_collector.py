#!/usr/bin/env python3
"""WebSocket trade tape collector for Polymarket US.

Subscribes to `SUBSCRIPTION_TYPE_TRADE` for the markets in
`data/poly_active_slugs.json` and writes every trade (with full maker
and taker side+intent) to a dedicated SQLite DB `data/poly_trade_tape.db`.

Why this exists:
    paper_vs_live_gap.md identified that paper's drain_queue() ignores
    taker side and over-fills 10x. Once we have a real trade tape with
    aggressor-side info, we can:
      1. Fix drain_queue() to count only trades whose taker.side matches
         our bid's side. (Or, more precisely, only trades where the
         taker is HITTING our bid — taker.side=='yes' && taker.intent
         =='sell' lifts a YES-bid maker order, etc.)
      2. Re-run the round-trip simulator against aggressor-aware queue
         dynamics.
      3. Compute VPIN per Bartlett & O'Hara as a real-time toxicity gate.

Operational:
    python scripts/trade_tape_collector.py
    # tails data/poly_active_slugs.json — refreshes subscription every
    # `--refresh-interval` seconds (default 15s).

Long-running. Recommended via pm2 or LaunchAgent. See module docstring
at the end for setup instructions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Iterable

# Allow importing src.* if needed (kept consistent with other scripts)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Defer SDK import to runtime — tests don't need it
_SDK_IMPORTED = False


def _detect_data_root() -> Path:
    """Find the canonical data/ dir (where the bot writes mm_live.db).

    A git worktree has its own data/ dir which the bot does NOT use; the
    bot writes to the main repo's data/ regardless of which worktree's
    script was invoked. So walk up the filesystem from this script
    looking for a data/ dir that contains poly_mm_live.db.

    Override with $POLYMARKET_DATA_DIR or --slugs/--db flags.
    """
    env_dir = os.environ.get("POLYMARKET_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "data" / "poly_mm_live.db"
        if candidate.exists():
            return candidate.parent
    # Last resort: script_dir/../data (creates new if needed)
    return Path(__file__).resolve().parent.parent / "data"


DATA_ROOT = _detect_data_root()
DEFAULT_DB_PATH = DATA_ROOT / "poly_trade_tape.db"
DEFAULT_SLUGS_PATH = DATA_ROOT / "poly_active_slugs.json"


# ---------------------------------------------------------------------------
# Aggressor-side enum decode (Polymarket US fully-qualified enums)
# ---------------------------------------------------------------------------
#
# The SDK emits side/intent as fully-qualified enum strings. The combination
# tells us which leg of the YES/NO book the taker was aggressing into:
#
#   taker.side    taker.intent             interpretation
#   ----------    ----------------------   --------------------------------
#   SELL          BUY_SHORT                taker sold YES (= bought NO); hit a YES_BID maker
#   BUY           BUY_LONG                 taker bought YES; lifted a YES_ASK maker
#   SELL          SELL_LONG                taker sold YES (closing long); hit a YES_BID maker
#   BUY           SELL_SHORT               taker bought YES (closing short); lifted a YES_ASK maker
#   SELL          BUY_LONG                 taker sold NO (= bought YES); hit a NO_BID maker
#   BUY           BUY_SHORT                taker bought NO; lifted a NO_ASK maker
#
# The functions below normalize to a simple "yes_buyer" / "yes_seller" /
# "no_buyer" / "no_seller" classification for analysis.

def taker_yes_or_no(taker_side: str, taker_intent: str) -> str:
    """Return 'yes' or 'no' for which side of the orderbook the taker hit.

    Returns empty string if undetermined. Used by drain_queue analysis to
    count only trades whose aggressor matched our bid's side.
    """
    s, i = taker_side or "", taker_intent or ""
    # Direct mapping: intent contains LONG → YES side; SHORT → NO side
    if "LONG" in i:
        return "yes"
    if "SHORT" in i:
        return "no"
    return ""


def taker_buy_or_sell(taker_side: str) -> str:
    """'buy' if taker lifted the ask, 'sell' if taker hit the bid."""
    if "BUY" in (taker_side or ""):
        return "buy"
    if "SELL" in (taker_side or ""):
        return "sell"
    return ""


# ---------------------------------------------------------------------------
# Pure functions (tested in tests/test_trade_tape_collector.py)
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    """Create the mm_trade_tape table and its slug+time index. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mm_trade_tape (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            trade_time TEXT NOT NULL,
            maker_side TEXT NOT NULL,
            maker_intent TEXT NOT NULL,
            taker_side TEXT NOT NULL,
            taker_intent TEXT NOT NULL,
            raw_json TEXT,
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trade_tape_slug_time
        ON mm_trade_tape(market_slug, trade_time)
    """)
    conn.commit()


def parse_trade_message(msg: dict) -> dict:
    """Convert SDK Trade message → row dict suitable for `persist_trade`.

    Raises ValueError if `msg` is not a Trade message.
    """
    if not isinstance(msg, dict) or "trade" not in msg:
        raise ValueError(f"Not a Trade message: keys={list(msg) if isinstance(msg, dict) else type(msg)}")
    trade = msg["trade"]
    price_value = trade.get("price", {}).get("value", "0")
    qty_value = trade.get("quantity", {}).get("value", "0")
    try:
        price_cents = round(float(price_value) * 100)
    except (TypeError, ValueError):
        price_cents = 0
    try:
        quantity = int(float(qty_value))
    except (TypeError, ValueError):
        quantity = 0
    maker = trade.get("maker") or {}
    taker = trade.get("taker") or {}
    return {
        "market_slug": trade.get("marketSlug", ""),
        "price_cents": price_cents,
        "quantity": quantity,
        "trade_time": trade.get("tradeTime", ""),
        "maker_side": maker.get("side", ""),
        "maker_intent": maker.get("intent", ""),
        "taker_side": taker.get("side", ""),
        "taker_intent": taker.get("intent", ""),
        "raw_json": json.dumps(msg, separators=(",", ":")),
    }


def persist_trade(conn: sqlite3.Connection, row: dict) -> int:
    """Insert a parsed trade row. Returns the new row's id."""
    cur = conn.execute("""
        INSERT INTO mm_trade_tape (
            market_slug, price_cents, quantity, trade_time,
            maker_side, maker_intent, taker_side, taker_intent, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["market_slug"], row["price_cents"], row["quantity"],
        row["trade_time"], row["maker_side"], row["maker_intent"],
        row["taker_side"], row["taker_intent"], row["raw_json"],
    ))
    conn.commit()
    return cur.lastrowid


def compute_slug_diff(current: set[str], target: set[str]) -> tuple[set[str], set[str]]:
    """Return (to_add, to_remove) needed to migrate `current` → `target`."""
    return (target - current, current - target)


def read_active_slugs(path: str | Path) -> set[str]:
    """Read the bot's active slug list. Returns empty set on missing/malformed file."""
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get("active_slugs") or [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


# ---------------------------------------------------------------------------
# Async collector (not unit-tested; integration-tested via main())
# ---------------------------------------------------------------------------

def _ensure_sdk_imported():
    global _SDK_IMPORTED
    if _SDK_IMPORTED:
        return
    global AsyncPolymarketUS
    from polymarket_us import AsyncPolymarketUS  # noqa: F401
    _SDK_IMPORTED = True


class TradeTapeCollector:
    """Long-running asyncio collector.

    Workflow:
      1. Open DB, init schema.
      2. Connect to MarketsWebSocket.
      3. Read current slugs from poly_active_slugs.json.
      4. Subscribe to SUBSCRIPTION_TYPE_TRADE for those slugs.
      5. On 'trade' event: parse + persist.
      6. Every `refresh_interval` seconds: re-read slugs and unsubscribe
         old / subscribe new as needed.
      7. On disconnect: reconnect with exponential backoff.
      8. On SIGINT/SIGTERM: graceful shutdown.
    """

    def __init__(self, *, key_id: str, secret_key: str,
                 db_path: str | Path, slugs_path: str | Path,
                 refresh_interval: float = 15.0,
                 base_url: str = "https://api.polymarket.us",
                 logger: logging.Logger | None = None):
        _ensure_sdk_imported()
        self.key_id = key_id
        self.secret_key = secret_key
        self.db_path = str(db_path)
        self.slugs_path = str(slugs_path)
        self.refresh_interval = refresh_interval
        self.base_url = base_url
        self.log = logger or logging.getLogger("trade_tape")
        self.client = None
        self.ws = None
        self.conn: sqlite3.Connection | None = None
        # Each subscribe request gets a unique requestId — needed for unsubscribe
        self.subscriptions: dict[str, str] = {}  # slug -> request_id
        self._stop = asyncio.Event()
        self._counters = {"trades_received": 0, "trades_persisted": 0, "errors": 0,
                          "reconnects": 0, "heartbeats": 0}

    async def run(self):
        self.conn = sqlite3.connect(self.db_path)
        init_db(self.conn)
        self.log.info(f"db ready: {self.db_path}")

        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_collect()
                backoff = 1.0  # reset on clean disconnect
            except Exception as e:
                self._counters["errors"] += 1
                self.log.error(f"collector error: {e}")
                self._counters["reconnects"] += 1
                await asyncio.sleep(min(backoff, 60.0))
                backoff *= 2

        # Graceful shutdown
        if self.ws and self.ws.is_connected:
            await self.ws.close()
        if self.conn:
            self.conn.close()
        self.log.info(f"shutdown. final stats: {self._counters}")

    async def _connect_and_collect(self):
        from polymarket_us import AsyncPolymarketUS
        self.client = AsyncPolymarketUS(
            key_id=self.key_id, secret_key=self.secret_key,
            api_base_url=self.base_url,
        )
        # ws.markets() is a method (returns a fresh MarketsWebSocket).
        # The factory rewrites https→wss internally.
        self.ws = self.client.ws.markets()
        self.ws.on("trade", self._on_trade)
        self.ws.on("error", self._on_error)
        self.ws.on("heartbeat", self._on_heartbeat)

        await self.ws.connect()
        self.log.info("ws connected")

        # Initial subscribe
        await self._refresh_subscriptions()

        # Periodic refresh loop
        while not self._stop.is_set() and self.ws.is_connected:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            if self.ws.is_connected:
                await self._refresh_subscriptions()
            else:
                self.log.warning("ws disconnected; will reconnect")
                break

    async def _refresh_subscriptions(self):
        target = read_active_slugs(self.slugs_path)
        current = set(self.subscriptions.keys())
        to_add, to_remove = compute_slug_diff(current, target)
        for slug in to_remove:
            req_id = self.subscriptions.pop(slug, None)
            if req_id:
                try:
                    await self.ws.unsubscribe(req_id)
                except Exception as e:
                    self.log.warning(f"unsubscribe({slug}) failed: {e}")
        if to_add:
            # Subscribe everything new with a single request to minimize messages
            req_id = f"trade-{uuid.uuid4().hex[:12]}"
            try:
                await self.ws.subscribe_trades(req_id, sorted(to_add))
                for slug in to_add:
                    self.subscriptions[slug] = req_id
                self.log.info(f"subscribed +{len(to_add)} slugs (now {len(self.subscriptions)})")
            except Exception as e:
                self.log.error(f"subscribe failed: {e}")

    def _on_trade(self, message):
        self._counters["trades_received"] += 1
        try:
            row = parse_trade_message(message)
            persist_trade(self.conn, row)
            self._counters["trades_persisted"] += 1
        except Exception as e:
            self._counters["errors"] += 1
            self.log.error(f"persist failed: {e} msg={message}")

    def _on_error(self, err):
        self._counters["errors"] += 1
        self.log.error(f"ws error: {err}")

    def _on_heartbeat(self):
        self._counters["heartbeats"] += 1

    def stop(self):
        self._stop.set()


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"ERROR: env var {name} is required", file=sys.stderr)
        sys.exit(2)
    return val


async def _amain(args):
    from dotenv import load_dotenv
    load_dotenv()
    key_id = _env("POLYMARKET_KEY_ID")
    secret_key = _env("POLYMARKET_SECRET_KEY")

    collector = TradeTapeCollector(
        key_id=key_id, secret_key=secret_key,
        db_path=args.db, slugs_path=args.slugs,
        refresh_interval=args.refresh_interval,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, collector.stop)

    if args.duration:
        async def stopper():
            await asyncio.sleep(args.duration)
            collector.stop()
        loop.create_task(stopper())

    await collector.run()


def main():
    parser = argparse.ArgumentParser(description="Polymarket trade tape collector")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH),
                        help="SQLite path for trade tape DB")
    parser.add_argument("--slugs", default=str(DEFAULT_SLUGS_PATH),
                        help="Path to poly_active_slugs.json")
    parser.add_argument("--refresh-interval", type=float, default=15.0,
                        help="Seconds between slug-set refreshes (default 15)")
    parser.add_argument("--duration", type=int, default=None,
                        help="Run for N seconds then exit (for integration testing)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
