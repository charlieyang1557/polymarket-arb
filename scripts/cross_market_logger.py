#!/usr/bin/env python3
"""Continuously log Polymarket orderbooks across correlated sports markets."""

import os
import signal
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.poly_client import PolyClient


DB_PATH = Path("data/cross_market_log.db")
POLL_INTERVAL_SECONDS = 30
SUMMARY_INTERVAL_SECONDS = 600


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def classify_market_type(slug: str) -> str | None:
    """Infer market type from the Polymarket slug prefix."""
    prefix = (slug or "").split("-", 1)[0].lower()
    return {
        "aec": "moneyline",
        "asc": "spread",
        "tsc": "totals",
    }.get(prefix)


def require_credentials() -> tuple[str, str]:
    """Load SDK credentials from the environment."""
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret_key = os.getenv("POLYMARKET_SECRET_KEY")
    if not key_id or not secret_key:
        raise SystemExit(
            "Missing POLYMARKET_KEY_ID or POLYMARKET_SECRET_KEY in environment/.env"
        )
    return key_id, secret_key


def init_db(db_path: Path) -> None:
    """Create the snapshots table if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                timestamp TEXT,
                event_slug TEXT,
                market_slug TEXT,
                market_type TEXT,
                best_bid INTEGER,
                best_bid_size INTEGER,
                best_ask INTEGER,
                best_ask_size INTEGER,
                mid_price REAL
            )
            """
        )


def extract_best_prices(orderbook: dict) -> tuple[int | None, int | None, int | None, int | None]:
    """Extract best YES bid/ask and sizes in cents from normalized orderbook."""
    orderbook_fp = orderbook.get("orderbook_fp") or {}
    yes_bids = orderbook_fp.get("yes_dollars") or []
    no_bids = orderbook_fp.get("no_dollars") or []

    best_bid = None
    best_bid_size = None
    if yes_bids:
        try:
            best_bid = round(float(yes_bids[-1][0]) * 100)
            best_bid_size = int(float(yes_bids[-1][1]))
        except (TypeError, ValueError, IndexError):
            best_bid = None
            best_bid_size = None

    best_ask = None
    best_ask_size = None
    if no_bids:
        try:
            best_no_bid = float(no_bids[-1][0])
            best_ask = round((1.0 - best_no_bid) * 100)
            best_ask_size = int(float(no_bids[-1][1]))
        except (TypeError, ValueError, IndexError):
            best_ask = None
            best_ask_size = None

    return best_bid, best_bid_size, best_ask, best_ask_size


def compute_mid_price(best_bid: int | None, best_ask: int | None) -> float | None:
    """Compute midpoint in cents."""
    if best_bid is None or best_ask is None:
        return None
    return round((best_bid + best_ask) / 2, 2)


def fetch_tracked_markets(client: PolyClient) -> dict[str, list[dict]]:
    """Fetch active sports events and group trackable markets by event."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    offset = 0
    page_size = 100
    tracked: dict[str, list[dict]] = defaultdict(list)

    while True:
        response = client.client.events.list(
            {
                "limit": page_size,
                "offset": offset,
                "startTimeMin": cutoff,
            }
        )
        events = response.get("events", []) if isinstance(response, dict) else []
        if not events:
            break

        for event in events:
            event_slug = event.get("slug") or event.get("title")
            markets = event.get("markets") or []
            if not event_slug or len(markets) < 2:
                continue

            for market in markets:
                market_slug = market.get("slug")
                market_type = classify_market_type(market_slug or "")
                if not market_slug or not market_type:
                    continue
                tracked[event_slug].append(
                    {
                        "event_slug": event_slug,
                        "market_slug": market_slug,
                        "market_type": market_type,
                    }
                )

        offset += page_size
        if len(events) < page_size:
            break
        time.sleep(0.1)

    return {
        event_slug: markets
        for event_slug, markets in tracked.items()
        if len(markets) >= 2
    }


def insert_snapshot(conn: sqlite3.Connection, row: tuple) -> None:
    """Insert one snapshot row."""
    conn.execute(
        """
        INSERT INTO snapshots (
            timestamp, event_slug, market_slug, market_type,
            best_bid, best_bid_size, best_ask, best_ask_size, mid_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )


def main() -> None:
    key_id, secret_key = require_credentials()
    client = PolyClient(key_id=key_id, secret_key=secret_key)

    print("Fetching active multi-market sports events...")
    tracked = fetch_tracked_markets(client)
    market_count = sum(len(markets) for markets in tracked.values())
    print(f"Tracking {market_count} markets across {len(tracked)} events")

    init_db(DB_PATH)
    shutdown_requested = False
    total_snapshots = 0
    first_timestamp = None
    last_timestamp = None
    last_summary_at = time.time()

    def _request_shutdown(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal shutdown_requested
        shutdown_requested = True
        print("\nShutdown requested, finishing current poll...")

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    with sqlite3.connect(DB_PATH) as conn:
        while not shutdown_requested:
            poll_started = time.time()
            timestamp = utc_now_iso()

            for event_slug, markets in tracked.items():
                for market in markets:
                    orderbook = client.get_orderbook(market["market_slug"])
                    best_bid, best_bid_size, best_ask, best_ask_size = extract_best_prices(
                        orderbook
                    )
                    mid_price = compute_mid_price(best_bid, best_ask)

                    insert_snapshot(
                        conn,
                        (
                            timestamp,
                            event_slug,
                            market["market_slug"],
                            market["market_type"],
                            best_bid,
                            best_bid_size,
                            best_ask,
                            best_ask_size,
                            mid_price,
                        ),
                    )
                    total_snapshots += 1
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    last_timestamp = timestamp
                    time.sleep(0.05)

            conn.commit()

            now = time.time()
            if now - last_summary_at >= SUMMARY_INTERVAL_SECONDS:
                print(
                    f"Logged {total_snapshots} snapshots across "
                    f"{len(tracked)} events, {market_count} markets"
                )
                last_summary_at = now

            elapsed = time.time() - poll_started
            if elapsed < POLL_INTERVAL_SECONDS and not shutdown_requested:
                time.sleep(POLL_INTERVAL_SECONDS - elapsed)

    print(f"Total snapshots: {total_snapshots}")
    print(f"Time range: {first_timestamp or 'n/a'} -> {last_timestamp or 'n/a'}")


if __name__ == "__main__":
    main()
