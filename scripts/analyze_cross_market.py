#!/usr/bin/env python3
"""Analyze lagged moves across correlated Polymarket sports markets."""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


MOVE_THRESHOLD_A = 3.0
MOVE_THRESHOLD_B = 2.0
PAIR_ORDER = [
    ("moneyline", "spread"),
    ("moneyline", "totals"),
    ("spread", "totals"),
]
BUCKET_ORDER = ["0-30s", "30-60s", "60-120s", "120-300s", "300s+", "never"]
SPORT_ORDER = ["NBA", "MLB", "NHL"]


def parse_timestamp(timestamp: str) -> datetime:
    """Parse UTC ISO-8601 timestamps stored in the logger DB."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def infer_market_type(market_slug: str) -> str | None:
    """Map Polymarket slug prefixes to research market types."""
    prefix = (market_slug or "").split("-", 1)[0].lower()
    return {
        "aec": "moneyline",
        "asc": "spread",
        "tsc": "totals",
    }.get(prefix)


def infer_sport(market_slug: str) -> str | None:
    """Infer sport bucket from the slug token after the market prefix."""
    parts = (market_slug or "").split("-")
    if len(parts) < 2:
        return None
    sport = parts[1].lower()
    return {
        "nba": "NBA",
        "mlb": "MLB",
        "nhl": "NHL",
    }.get(sport)


def group_snapshots_by_event(rows: list[dict]) -> dict[str, list[dict]]:
    """Group snapshot rows by event slug while preserving row order."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        event_slug = row.get("event_slug")
        if event_slug:
            grouped[event_slug].append(row)
    return dict(grouped)


def build_market_series(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by market slug and sort by timestamp."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        market_slug = row.get("market_slug")
        if market_slug:
            grouped[market_slug].append(row)

    for market_rows in grouped.values():
        market_rows.sort(key=lambda row: parse_timestamp(row["timestamp"]))

    return dict(grouped)


def consecutive_moves(
    snapshots: list[dict], threshold_cents: float
) -> list[dict]:
    """Return moves that exceed the threshold between consecutive snapshots."""
    moves = []
    for previous, current in zip(snapshots, snapshots[1:]):
        prev_mid = previous.get("mid_price")
        curr_mid = current.get("mid_price")
        if prev_mid is None or curr_mid is None:
            continue

        change = float(curr_mid) - float(prev_mid)
        if abs(change) < threshold_cents:
            continue

        moves.append(
            {
                "timestamp": parse_timestamp(current["timestamp"]),
                "change": change,
                "direction": 1 if change > 0 else -1,
            }
        )
    return moves


def detect_market_lag(
    market_a: list[dict],
    market_b: list[dict],
    *,
    trigger_threshold: float = MOVE_THRESHOLD_A,
    response_threshold: float = MOVE_THRESHOLD_B,
) -> float | None:
    """Return the first lag in seconds from A's trigger move to B's response."""
    lags = detect_market_lags(
        market_a,
        market_b,
        trigger_threshold=trigger_threshold,
        response_threshold=response_threshold,
    )
    return lags[0] if lags else None


def detect_market_lags(
    market_a: list[dict],
    market_b: list[dict],
    *,
    trigger_threshold: float = MOVE_THRESHOLD_A,
    response_threshold: float = MOVE_THRESHOLD_B,
) -> list[float]:
    """Return lag seconds for every qualifying move in A against B."""
    lags = []
    responses = consecutive_moves(market_b, response_threshold)

    for trigger in consecutive_moves(market_a, trigger_threshold):
        match = next(
            (
                response
                for response in responses
                if response["timestamp"] >= trigger["timestamp"]
                and response["direction"] == trigger["direction"]
            ),
            None,
        )
        if match is not None:
            lag = (match["timestamp"] - trigger["timestamp"]).total_seconds()
            lags.append(lag)

    return lags


def bucket_for_lag(lag_seconds: float | None) -> str:
    """Map lag seconds into report buckets."""
    if lag_seconds is None:
        return "never"
    if lag_seconds < 30:
        return "0-30s"
    if lag_seconds < 60:
        return "30-60s"
    if lag_seconds < 120:
        return "60-120s"
    if lag_seconds < 300:
        return "120-300s"
    return "300s+"


def analyze_event(
    event_slug: str, rows: list[dict], pair_order: list[tuple[str, str]] | None = None
) -> list[dict]:
    """Analyze all configured market-type pairs for a single event."""
    pair_order = pair_order or PAIR_ORDER
    market_series = build_market_series(rows)
    markets_by_type: dict[str, list[tuple[str, list[dict]]]] = defaultdict(list)

    for market_slug, series in market_series.items():
        market_type = series[0].get("market_type") or infer_market_type(market_slug)
        if not market_type:
            continue
        markets_by_type[market_type].append((market_slug, series))

    sport = None
    if market_series:
        any_market_slug = next(iter(market_series))
        sport = infer_sport(any_market_slug)

    results = []
    for source_type, target_type in pair_order:
        for (source_slug, source_series), (target_slug, target_series) in product(
            markets_by_type.get(source_type, []),
            markets_by_type.get(target_type, []),
        ):
            lags = detect_market_lags(source_series, target_series)
            if lags:
                for lag in lags:
                    results.append(
                        {
                            "event_slug": event_slug,
                            "sport": sport,
                            "source_market_slug": source_slug,
                            "target_market_slug": target_slug,
                            "pair": f"{source_type}->{target_type}",
                            "lag_seconds": lag,
                            "bucket": bucket_for_lag(lag),
                        }
                    )
            else:
                results.append(
                    {
                        "event_slug": event_slug,
                        "sport": sport,
                        "source_market_slug": source_slug,
                        "target_market_slug": target_slug,
                        "pair": f"{source_type}->{target_type}",
                        "lag_seconds": None,
                        "bucket": "never",
                    }
                )

    return results


def summarize_results(results: list[dict]) -> dict[tuple[str, str], dict[str, int]]:
    """Count lag buckets by (sport, market-type-pair)."""
    summary: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {bucket: 0 for bucket in BUCKET_ORDER}
    )
    for result in results:
        sport = result.get("sport") or "Unknown"
        pair = result["pair"]
        summary[(sport, pair)][result["bucket"]] += 1
    return dict(summary)


def load_snapshots(db_path: str) -> list[dict]:
    """Load snapshot rows from SQLite."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT timestamp, event_slug, market_slug, market_type,
                   best_bid, best_bid_size, best_ask, best_ask_size, mid_price
            FROM snapshots
            ORDER BY event_slug, market_slug, timestamp
            """
        ).fetchall()
    return [dict(row) for row in rows]


def render_summary(summary: dict[tuple[str, str], dict[str, int]]) -> str:
    """Render a compact text report."""
    lines = []
    ordered_keys = sorted(
        summary,
        key=lambda item: (
            SPORT_ORDER.index(item[0]) if item[0] in SPORT_ORDER else len(SPORT_ORDER),
            PAIR_ORDER.index(tuple(item[1].split("->")))
            if tuple(item[1].split("->")) in PAIR_ORDER
            else len(PAIR_ORDER),
            item,
        ),
    )
    for sport, pair in ordered_keys:
        counts = summary[(sport, pair)]
        bucket_text = ", ".join(f"{bucket}={counts[bucket]}" for bucket in BUCKET_ORDER)
        lines.append(f"{sport} {pair}: {bucket_text}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="data/cross_market_log.db",
        help="Path to the SQLite DB created by cross_market_logger.py",
    )
    args = parser.parse_args()

    rows = load_snapshots(args.db)
    grouped = group_snapshots_by_event(rows)

    results = []
    for event_slug, event_rows in grouped.items():
        results.extend(analyze_event(event_slug, event_rows))

    summary = summarize_results(results)
    if not summary:
        print("No analyzable cross-market pairs found.")
        return

    print(render_summary(summary))


if __name__ == "__main__":
    main()
