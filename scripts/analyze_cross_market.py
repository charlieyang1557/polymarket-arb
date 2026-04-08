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
FOLLOWER_WINDOW_SECONDS = 300


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


def _sorted_snapshots(snapshots: list[dict]) -> list[dict]:
    """Return snapshots sorted by timestamp."""
    return sorted(snapshots, key=lambda row: parse_timestamp(row["timestamp"]))


def _window_baseline(
    trigger_timestamp: datetime, snapshots: list[dict]
) -> tuple[float | None, list[dict]]:
    """Return the follower baseline mid and snapshots inside the analysis window."""
    baseline_mid = None
    ordered = _sorted_snapshots(snapshots)
    for snapshot in ordered:
        snapshot_time = parse_timestamp(snapshot["timestamp"])
        mid_price = snapshot.get("mid_price")
        if snapshot_time <= trigger_timestamp and mid_price is not None:
            baseline_mid = float(mid_price)
            continue
        if snapshot_time > trigger_timestamp:
            break

    window_end = trigger_timestamp.timestamp() + FOLLOWER_WINDOW_SECONDS
    window_snapshots = [
        snapshot
        for snapshot in ordered
        if trigger_timestamp < parse_timestamp(snapshot["timestamp"])
        and parse_timestamp(snapshot["timestamp"]).timestamp() <= window_end
    ]

    if baseline_mid is None and window_snapshots:
        first_mid = window_snapshots[0].get("mid_price")
        if first_mid is not None:
            baseline_mid = float(first_mid)
            window_snapshots = window_snapshots[1:]

    return baseline_mid, window_snapshots


def classify_follower_outcome(
    trigger: dict,
    follower_snapshots: list[dict],
    *,
    response_threshold: float = MOVE_THRESHOLD_B,
) -> dict:
    """Classify the follower's move within five minutes of a leader trigger."""
    trigger_timestamp = trigger["timestamp"]
    baseline_mid, window_snapshots = _window_baseline(trigger_timestamp, follower_snapshots)
    if baseline_mid is None:
        return {"outcome": "flat", "follower_change": None, "lag_seconds": None}

    for snapshot in window_snapshots:
        mid_price = snapshot.get("mid_price")
        if mid_price is None:
            continue
        follower_change = float(mid_price) - baseline_mid
        if abs(follower_change) < response_threshold:
            continue

        lag_seconds = (
            parse_timestamp(snapshot["timestamp"]) - trigger_timestamp
        ).total_seconds()
        outcome = "correct" if follower_change * trigger["direction"] > 0 else "wrong"
        return {
            "outcome": outcome,
            "follower_change": round(follower_change, 4),
            "lag_seconds": lag_seconds,
        }

    return {"outcome": "flat", "follower_change": None, "lag_seconds": None}


def analyze_event_direction(
    event_slug: str, rows: list[dict], pair_order: list[tuple[str, str]] | None = None
) -> list[dict]:
    """Analyze direction accuracy for all configured market pairs in an event."""
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
        sport = infer_sport(next(iter(market_series)))

    results = []
    for source_type, target_type in pair_order:
        for (source_slug, source_series), (target_slug, target_series) in product(
            markets_by_type.get(source_type, []),
            markets_by_type.get(target_type, []),
        ):
            for trigger in consecutive_moves(source_series, MOVE_THRESHOLD_A):
                outcome = classify_follower_outcome(trigger, target_series)
                results.append(
                    {
                        "event_slug": event_slug,
                        "sport": sport,
                        "source_market_slug": source_slug,
                        "target_market_slug": target_slug,
                        "pair": f"{source_type}->{target_type}",
                        "trigger_timestamp": trigger["timestamp"],
                        "trigger_direction": trigger["direction"],
                        "trigger_change": trigger["change"],
                        **outcome,
                    }
                )
    return results


def direction_accuracy_report(results: list[dict]) -> dict[tuple[str, str], dict]:
    """Summarize direction outcomes by sport and market pair."""
    grouped: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "leader_up_correct": 0,
            "leader_up_wrong": 0,
            "leader_up_flat": 0,
            "leader_down_correct": 0,
            "leader_down_wrong": 0,
            "leader_down_flat": 0,
            "correct_count": 0,
            "wrong_count": 0,
            "flat_count": 0,
            "correct_magnitudes": [],
            "wrong_magnitudes": [],
            "correct_lags": [],
            "event_slugs": set(),
        }
    )

    for result in results:
        sport = result.get("sport") or "Unknown"
        pair = result["pair"]
        bucket = grouped[(sport, pair)]
        bucket["event_slugs"].add(result["event_slug"])

        prefix = "leader_up" if result["trigger_direction"] > 0 else "leader_down"
        outcome = result["outcome"]
        bucket[f"{prefix}_{outcome}"] += 1
        bucket[f"{outcome}_count"] += 1

        follower_change = result.get("follower_change")
        if outcome == "correct" and follower_change is not None:
            bucket["correct_magnitudes"].append(abs(float(follower_change)))
        if outcome == "wrong" and follower_change is not None:
            bucket["wrong_magnitudes"].append(abs(float(follower_change)))
        if outcome == "correct" and result.get("lag_seconds") is not None:
            bucket["correct_lags"].append(float(result["lag_seconds"]))

    summary = {}
    for key, bucket in grouped.items():
        trigger_count = (
            bucket["correct_count"] + bucket["wrong_count"] + bucket["flat_count"]
        )
        non_flat_count = bucket["correct_count"] + bucket["wrong_count"]
        summary[key] = {
            "leader_up_correct": bucket["leader_up_correct"],
            "leader_up_wrong": bucket["leader_up_wrong"],
            "leader_up_flat": bucket["leader_up_flat"],
            "leader_down_correct": bucket["leader_down_correct"],
            "leader_down_wrong": bucket["leader_down_wrong"],
            "leader_down_flat": bucket["leader_down_flat"],
            "direction_accuracy_pct": (
                100 * bucket["correct_count"] / trigger_count if trigger_count else None
            ),
            "direction_accuracy_ex_flat_pct": (
                100 * bucket["correct_count"] / non_flat_count
                if non_flat_count
                else None
            ),
            "avg_follower_magnitude_correct": (
                sum(bucket["correct_magnitudes"]) / len(bucket["correct_magnitudes"])
                if bucket["correct_magnitudes"]
                else None
            ),
            "avg_follower_magnitude_wrong": (
                sum(bucket["wrong_magnitudes"]) / len(bucket["wrong_magnitudes"])
                if bucket["wrong_magnitudes"]
                else None
            ),
            "avg_lag_correct_seconds": (
                sum(bucket["correct_lags"]) / len(bucket["correct_lags"])
                if bucket["correct_lags"]
                else None
            ),
            "trigger_count": trigger_count,
            "event_count": len(bucket["event_slugs"]),
        }
    return dict(summary)


def taker_fee_cents(price_cents: float) -> float:
    """Return Polymarket sports taker fee in cents per contract."""
    probability = price_cents / 100
    return round(0.02 * probability * (1 - probability) * 100, 4)


def simulate_trade(
    trigger: dict,
    follower_snapshots: list[dict],
    *,
    take_profit_threshold: float = MOVE_THRESHOLD_B,
) -> dict:
    """Simulate a directional follower trade entered one snapshot after the trigger."""
    trigger_timestamp = trigger["timestamp"]
    ordered = _sorted_snapshots(follower_snapshots)
    entry_snapshot = next(
        (
            snapshot
            for snapshot in ordered
            if parse_timestamp(snapshot["timestamp"]) > trigger_timestamp
        ),
        None,
    )
    if entry_snapshot is None:
        return {
            "entered": False,
            "entry_price": None,
            "exit_price": None,
            "entry_fee": None,
            "exit_fee": None,
            "pnl_cents": None,
            "exit_reason": "no_entry",
        }

    direction = trigger["direction"]
    entry_mid = entry_snapshot.get("mid_price")
    if entry_mid is None:
        return {
            "entered": False,
            "entry_price": None,
            "exit_price": None,
            "entry_fee": None,
            "exit_fee": None,
            "pnl_cents": None,
            "exit_reason": "no_entry",
        }

    if direction > 0:
        entry_quote = entry_snapshot.get("best_ask")
    else:
        best_bid = entry_snapshot.get("best_bid")
        entry_quote = 100 - float(best_bid) if best_bid is not None else None

    if entry_quote is None:
        return {
            "entered": False,
            "entry_price": None,
            "exit_price": None,
            "entry_fee": None,
            "exit_fee": None,
            "pnl_cents": None,
            "exit_reason": "no_entry",
        }

    entry_price = float(entry_quote)
    entry_time = parse_timestamp(entry_snapshot["timestamp"])
    window_end = trigger_timestamp.timestamp() + FOLLOWER_WINDOW_SECONDS

    exit_snapshot = entry_snapshot
    exit_reason = "timeout"
    for snapshot in ordered:
        snapshot_time = parse_timestamp(snapshot["timestamp"])
        if snapshot_time <= entry_time or snapshot_time.timestamp() > window_end:
            continue
        mid_price = snapshot.get("mid_price")
        if mid_price is None:
            continue
        directional_move = (float(mid_price) - float(entry_mid)) * direction
        exit_snapshot = snapshot
        if directional_move >= take_profit_threshold:
            exit_reason = "take_profit"
            break

    exit_mid = exit_snapshot.get("mid_price")
    if exit_mid is None:
        return {
            "entered": False,
            "entry_price": None,
            "exit_price": None,
            "entry_fee": None,
            "exit_fee": None,
            "pnl_cents": None,
            "exit_reason": "no_exit_price",
        }

    exit_price = float(exit_mid) if direction > 0 else 100 - float(exit_mid)
    entry_fee = taker_fee_cents(entry_price)
    exit_fee = taker_fee_cents(exit_price)
    pnl_cents = round(exit_price - entry_price - entry_fee - exit_fee, 4)

    return {
        "entered": True,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "pnl_cents": pnl_cents,
        "exit_reason": exit_reason,
    }


def analyze_event_pnl(
    event_slug: str, rows: list[dict], pair_order: list[tuple[str, str]] | None = None
) -> list[dict]:
    """Simulate follower trades for all configured market pairs in an event."""
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
        sport = infer_sport(next(iter(market_series)))

    results = []
    for source_type, target_type in pair_order:
        for (source_slug, source_series), (target_slug, target_series) in product(
            markets_by_type.get(source_type, []),
            markets_by_type.get(target_type, []),
        ):
            for trigger in consecutive_moves(source_series, MOVE_THRESHOLD_A):
                trade = simulate_trade(trigger, target_series)
                results.append(
                    {
                        "event_slug": event_slug,
                        "sport": sport,
                        "source_market_slug": source_slug,
                        "target_market_slug": target_slug,
                        "pair": f"{source_type}->{target_type}",
                        "trigger_direction": trigger["direction"],
                        "trigger_change": trigger["change"],
                        **trade,
                    }
                )
    return results


def summarize_pnl_results(results: list[dict]) -> dict[tuple[str, str], dict]:
    """Aggregate simulated PnL results by sport and market pair."""
    grouped: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "trade_count": 0,
            "win_count": 0,
            "total_pnl_cents": 0.0,
            "trigger_count": 0,
            "event_slugs": set(),
        }
    )

    for result in results:
        sport = result.get("sport") or "Unknown"
        pair = result["pair"]
        bucket = grouped[(sport, pair)]
        bucket["trigger_count"] += 1
        bucket["event_slugs"].add(result["event_slug"])

        if not result.get("entered"):
            continue

        pnl_cents = result.get("pnl_cents")
        if pnl_cents is None:
            continue
        bucket["trade_count"] += 1
        bucket["total_pnl_cents"] += float(pnl_cents)
        if float(pnl_cents) > 0:
            bucket["win_count"] += 1

    summary = {}
    for key, bucket in grouped.items():
        trade_count = bucket["trade_count"]
        summary[key] = {
            "trade_count": trade_count,
            "win_rate_pct": (
                100 * bucket["win_count"] / trade_count if trade_count else None
            ),
            "avg_pnl_cents": (
                bucket["total_pnl_cents"] / trade_count if trade_count else None
            ),
            "total_pnl_cents": bucket["total_pnl_cents"],
            "trigger_count": bucket["trigger_count"],
            "event_count": len(bucket["event_slugs"]),
        }
    return dict(summary)


def _ordered_report_keys(summary: dict[tuple[str, str], dict]) -> list[tuple[str, str]]:
    """Return report keys ordered by sport and configured market pair."""
    return sorted(
        summary,
        key=lambda item: (
            SPORT_ORDER.index(item[0]) if item[0] in SPORT_ORDER else len(SPORT_ORDER),
            PAIR_ORDER.index(tuple(item[1].split("->")))
            if tuple(item[1].split("->")) in PAIR_ORDER
            else len(PAIR_ORDER),
            item,
        ),
    )


def _fmt_metric(value: float | None, *, digits: int = 1, suffix: str = "") -> str:
    """Format optional numeric metrics for text tables."""
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def render_direction_report(summary: dict[tuple[str, str], dict]) -> str:
    """Render direction accuracy summary by sport and pair."""
    if not summary:
        return "No direction-accuracy triggers found."

    lines = [
        "Direction Accuracy",
        "sport pair triggers events up(c/w/f) down(c/w/f) acc acc_ex_flat avg_mag_correct avg_mag_wrong avg_lag_correct",
    ]
    for sport, pair in _ordered_report_keys(summary):
        stats = summary[(sport, pair)]
        lines.append(
            " ".join(
                [
                    sport,
                    pair,
                    str(stats["trigger_count"]),
                    str(stats["event_count"]),
                    (
                        f'{stats["leader_up_correct"]}/'
                        f'{stats["leader_up_wrong"]}/'
                        f'{stats["leader_up_flat"]}'
                    ),
                    (
                        f'{stats["leader_down_correct"]}/'
                        f'{stats["leader_down_wrong"]}/'
                        f'{stats["leader_down_flat"]}'
                    ),
                    _fmt_metric(stats["direction_accuracy_pct"], suffix="%"),
                    _fmt_metric(stats["direction_accuracy_ex_flat_pct"], suffix="%"),
                    _fmt_metric(stats["avg_follower_magnitude_correct"], suffix="c"),
                    _fmt_metric(stats["avg_follower_magnitude_wrong"], suffix="c"),
                    _fmt_metric(stats["avg_lag_correct_seconds"], suffix="s"),
                ]
            )
        )
    return "\n".join(lines)


def render_pnl_report(summary: dict[tuple[str, str], dict]) -> str:
    """Render simulated PnL summary by sport and pair."""
    if not summary:
        return "No simulated trades found."

    lines = [
        "Simulated PnL",
        "sport pair triggers events trades win_rate avg_pnl total_pnl",
    ]
    for sport, pair in _ordered_report_keys(summary):
        stats = summary[(sport, pair)]
        lines.append(
            " ".join(
                [
                    sport,
                    pair,
                    str(stats["trigger_count"]),
                    str(stats["event_count"]),
                    str(stats["trade_count"]),
                    _fmt_metric(stats["win_rate_pct"], suffix="%"),
                    _fmt_metric(stats["avg_pnl_cents"], suffix="c"),
                    _fmt_metric(stats["total_pnl_cents"], suffix="c"),
                ]
            )
        )
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

    direction_results = []
    pnl_results = []
    for event_slug, event_rows in grouped.items():
        direction_results.extend(analyze_event_direction(event_slug, event_rows))
        pnl_results.extend(analyze_event_pnl(event_slug, event_rows))

    print()
    print(render_direction_report(direction_accuracy_report(direction_results)))
    print()
    print(render_pnl_report(summarize_pnl_results(pnl_results)))


if __name__ == "__main__":
    main()
