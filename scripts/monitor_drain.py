#!/usr/bin/env python3
"""Watchdog: independently verifies the paper MM bot is processing trades.

Runs every 5 minutes. For each market:
  - Fetches recent trades from Kalshi API
  - Reads bot's snapshots from the DB
  - Compares: if API shows trades but bot shows 0 volume → ALERT
  - Also detects stuck inventory (inv != 0 for 30+ min with no pnl change)

Usage:
    python scripts/monitor_drain.py                    # default DB + 5min
    python scripts/monitor_drain.py --db data/mm.db    # custom DB
    python scripts/monitor_drain.py --interval 60      # every 60s (testing)
"""

import argparse
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from src.kalshi_client import KalshiClient, PROD_BASE

TICKERS = [
    "KXGREENLAND-29",
    "KXTRUMPREMOVE",
    "KXGREENLANDPRICE-29JAN21-NOACQ",
    "KXVPRESNOMR-28-MR",
    "KXINSURRECTION-29-27",
]

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")


def discord_alert(msg: str):
    """Send alert to Discord and stderr."""
    print(f"🚨 {msg}", file=sys.stderr, flush=True)
    if DISCORD_WEBHOOK:
        try:
            import requests
            requests.post(DISCORD_WEBHOOK,
                          json={"content": f"🚨 **MM MONITOR** {msg}"},
                          timeout=5)
        except Exception:
            pass


def get_bot_session(conn: sqlite3.Connection) -> str | None:
    """Get the most recent session_id from snapshots."""
    row = conn.execute(
        "SELECT session_id FROM mm_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def get_bot_start_time(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Get earliest snapshot timestamp for this session."""
    row = conn.execute(
        "SELECT MIN(ts) FROM mm_snapshots WHERE session_id = ?",
        (session_id,)
    ).fetchone()
    return row[0] if row else None


def get_bot_volume(conn: sqlite3.Connection, session_id: str,
                   ticker: str, since_ts: str) -> int:
    """Max trade_volume_1min from snapshots in the window.

    trade_volume_1min is per-tick (not cumulative), so SUM would always be 0
    if trades only happen on one tick. Use MAX to detect any tick that saw trades.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(trade_volume_1min), 0) "
        "FROM mm_snapshots "
        "WHERE session_id = ? AND ticker = ? AND ts >= ?",
        (session_id, ticker, since_ts)
    ).fetchone()
    return row[0]


def get_inventory_history(conn: sqlite3.Connection, session_id: str,
                          ticker: str) -> list[tuple]:
    """Get (ts, net_inventory, realized_pnl) from last 35 minutes."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
    rows = conn.execute(
        "SELECT ts, net_inventory, realized_pnl "
        "FROM mm_snapshots "
        "WHERE session_id = ? AND ticker = ? AND ts >= ? "
        "ORDER BY ts",
        (session_id, ticker, cutoff)
    ).fetchall()
    return rows


def check_drain(client: KalshiClient, conn: sqlite3.Connection,
                session_id: str, bot_start: str):
    """Fetch API trades and delegate to check_drain_v2."""
    now = datetime.now(timezone.utc)
    min_ts = int(now.timestamp()) - 300

    ticker_trades = {}
    for ticker in TICKERS:
        try:
            data = client.get_trades(ticker, limit=100, min_ts=min_ts)
            trades = data.get("trades", [])
            # Only trades after bot started
            ticker_trades[ticker] = [
                t for t in trades
                if t.get("created_time", "") > bot_start
            ]
        except Exception as e:
            print(f"  {ticker}: API error ({e})", flush=True)
            ticker_trades[ticker] = []

    return check_drain_v2(conn, session_id, ticker_trades)


def check_drain_v2(conn: sqlite3.Connection, session_id: str,
                   ticker_trades: dict[str, list[dict]]) -> list[str]:
    """V2 drain check: hybrid API + snapshot comparison.

    Args:
        conn: DB connection
        session_id: current bot session
        ticker_trades: {ticker: [api_trade_dicts]} from Kalshi API

    Logic:
    - Get last 2 snapshots for each market (includes order prices)
    - If yes_queue_pos or no_queue_pos decreased: drain OK
    - Filter API trades to those at/below our order price (same as drain_queue)
    - If relevant API trades > 0 but queue unchanged: ALERT
    - If no relevant API trades: quiet (trades at other prices)
    """
    alerts = []

    for ticker, api_trades in ticker_trades.items():
        rows = conn.execute(
            "SELECT yes_queue_pos, no_queue_pos, "
            "       yes_order_price, no_order_price "
            "FROM mm_snapshots "
            "WHERE session_id = ? AND ticker = ? "
            "ORDER BY id DESC LIMIT 2",
            (session_id, ticker)
        ).fetchall()

        if len(rows) < 2:
            continue

        # rows[0] = newest, rows[1] = older
        new_yq, new_nq, yes_price, no_price = rows[0]
        old_yq, old_nq, _, _ = rows[1]

        # No orders placed → skip
        if yes_price is None and no_price is None:
            continue

        # Check if either queue position decreased
        yes_drained = (old_yq is not None and new_yq is not None
                       and new_yq < old_yq)
        no_drained = (old_nq is not None and new_nq is not None
                      and new_nq < old_nq)

        if yes_drained or no_drained:
            continue  # drain working

        # Queue unchanged — filter API trades to our price levels
        relevant_vol = 0
        for t in api_trades:
            yes_price_cents = round(
                float(t.get("yes_price_dollars", 0) or 0) * 100)
            count = float(t.get("count_fp", 0) or 0)

            # YES side: trade at/below our YES bid drains our queue
            if yes_price is not None and yes_price_cents <= yes_price:
                relevant_vol += count
            # NO side: trade at/below our NO bid drains our queue
            if no_price is not None:
                no_price_cents = 100 - yes_price_cents
                if no_price_cents <= no_price:
                    relevant_vol += count

        if relevant_vol > 0:
            alerts.append(
                f"DRAIN STALL on {ticker}: queue_pos unchanged, "
                f"API has {relevant_vol:.0f} contracts at our price level")

    return alerts


def check_stuck_inventory(conn: sqlite3.Connection, session_id: str):
    """Legacy stuck check — delegates to v2."""
    alerts, _ = check_stuck_inventory_v2(
        conn, session_id, TICKERS, getattr(check_stuck_inventory, '_alerted', set()))
    check_stuck_inventory._alerted = _  # persist across calls
    return alerts


def check_stuck_inventory_v2(conn: sqlite3.Connection, session_id: str,
                             tickers: list[str],
                             already_alerted: set[str]
                             ) -> tuple[list[str], set[str]]:
    """V2 stuck inventory check: 120min threshold, alert once per event.

    Returns (alerts, updated_already_alerted set).
    """
    alerts = []
    updated_alerted = set(already_alerted)

    for ticker in tickers:
        # Fetch snapshots from last 135 minutes
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=135)).isoformat()
        rows = conn.execute(
            "SELECT ts, net_inventory, realized_pnl "
            "FROM mm_snapshots "
            "WHERE session_id = ? AND ticker = ? AND ts >= ? "
            "ORDER BY ts",
            (session_id, ticker, cutoff)
        ).fetchall()

        if len(rows) < 2:
            continue

        inventories = [r[1] for r in rows]
        pnls = [r[2] for r in rows]

        # If inventory is zero or pnl changed, clear alert flag
        if inventories[-1] == 0 or pnls[0] != pnls[-1]:
            updated_alerted.discard(ticker)
            continue

        # All non-zero inventory with unchanged pnl
        if not all(inv != 0 for inv in inventories):
            updated_alerted.discard(ticker)
            continue

        oldest_ts = rows[0][0]
        newest_ts = rows[-1][0]
        try:
            t0 = datetime.fromisoformat(oldest_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(newest_ts.replace("Z", "+00:00"))
        except ValueError:
            continue

        span = (t1 - t0).total_seconds()
        if span < 7200:  # 120 minutes
            continue

        # Already alerted for this market → suppress
        if ticker in already_alerted:
            continue

        msg = (f"STUCK INVENTORY on {ticker}: "
               f"inv={inventories[-1]} for {span/60:.0f}min, "
               f"pnl unchanged at {pnls[-1]:.1f}c")
        alerts.append(msg)
        updated_alerted.add(ticker)

    return alerts, updated_alerted


def main():
    parser = argparse.ArgumentParser(description="MM drain watchdog")
    parser.add_argument("--db", default="data/mm_paper.db")
    parser.add_argument("--interval", type=int, default=300,
                        help="Check interval in seconds (default: 300)")
    args = parser.parse_args()

    api_key = os.getenv("KALSHI_API_KEY")
    pk_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pk_path:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH",
              file=sys.stderr)
        sys.exit(1)

    client = KalshiClient(api_key, pk_path, PROD_BASE)

    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"MM Monitor started | interval={args.interval}s | db={args.db}",
          flush=True)

    last_check_time = None
    stuck_alerted: set[str] = set()

    while not shutdown:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%H:%M:%S")

        # Detect system sleep: if last check was > 2x interval ago, skip
        # this cycle — the bot also slept and missed these trades
        if last_check_time:
            gap = (now - last_check_time).total_seconds()
            if gap > args.interval * 2:
                print(f"\n[{ts}] SYSTEM SLEEP detected "
                      f"(gap={gap/60:.0f}min, expected {args.interval}s). "
                      f"Skipping this check — bot also missed this window.",
                      flush=True)
                last_check_time = now
                time.sleep(args.interval)
                continue

        last_check_time = now

        # Open DB fresh each cycle (bot may be writing)
        try:
            conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            print(f"[{ts}] DB not ready: {e}", flush=True)
            time.sleep(args.interval)
            continue

        try:
            session_id = get_bot_session(conn)
            if not session_id:
                print(f"[{ts}] No snapshots yet — bot may still be starting",
                      flush=True)
                conn.close()
                time.sleep(args.interval)
                continue

            bot_start = get_bot_start_time(conn, session_id)
            print(f"\n[{ts}] Check | session={session_id}", flush=True)

            drain_alerts = check_drain(client, conn, session_id, bot_start)
            inv_alerts, stuck_alerted = check_stuck_inventory_v2(
                conn, session_id, TICKERS, stuck_alerted)

            for msg in drain_alerts + inv_alerts:
                discord_alert(msg)

            if not drain_alerts and not inv_alerts:
                print(f"[{ts}] All clear", flush=True)

        except Exception as e:
            print(f"[{ts}] Monitor error: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

        time.sleep(args.interval)

    print("Monitor stopped.", flush=True)


if __name__ == "__main__":
    main()
