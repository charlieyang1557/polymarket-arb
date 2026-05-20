#!/usr/bin/env python3
"""Round-trip simulator for the YES penalty counterfactual.

Replays the 326 historical live fills with a configurable YES-fill survival
model, processes them through a FIFO pair-off queue (matching the bot's
`pair_off_inventory` semantics), exits unpaired inventory at the last
snapshot's BBO (with taker fee), and reports net round-trip P&L per slice
(full / per prefix) under each survival model.

This is the round-trip companion to yes_penalty_counterfactual.py, which
only models hold-to-settle. The bot actually round-trips out before
settlement most of the time, so this is closer to the realized P&L the
penalty would have produced.

Run:
    python scripts/research/roundtrip_simulator.py
    python scripts/research/roundtrip_simulator.py --trials 500 --seed 0
    python scripts/research/roundtrip_simulator.py --filter-prefix tsc
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable

# Allow importing src.poly_client for fee calc
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.poly_client import calculate_maker_fee  # noqa: E402


DB_PATH = "/Users/openclaw/polymarket-arb/data/poly_mm_live.db"
SETTLEMENT_CACHE = (
    "/Users/openclaw/polymarket-arb/.claude/worktrees/"
    "charming-mcclintock-caff3f/data/research/resolved_markets_cache.json"
)

# Same three survival models as the static counterfactual — keep them in
# sync so the two reports are directly comparable.
SURVIVAL_MODELS = {
    "pessimistic": {0: 0.20, -1: 0.10, "+1_or_above": 0.50, "no_snapshot": 0.30},
    "base":        {0: 0.40, -1: 0.20, "+1_or_above": 0.80, "no_snapshot": 0.40},
    "optimistic":  {0: 0.60, -1: 0.40, "+1_or_above": 1.00, "no_snapshot": 0.60},
}


# ---------------------------------------------------------------------------
# Pure simulation kernel
# ---------------------------------------------------------------------------

def _taker_fee_per_contract(price_cents: int) -> float:
    """Polymarket sports taker fee: 0.02 * P * (1-P) * 100 cents."""
    p = price_cents / 100
    return 0.02 * p * (1 - p) * 100


def apply_survival_to_fills(fills, survival_fn: Callable[[dict], float],
                              rng: random.Random) -> list[dict]:
    """Filter `fills` by applying survival_fn to each fill.

    For each fill, draws a uniform [0,1) sample; keeps the fill if sample
    < survival_fn(fill). The survival_fn is responsible for returning 1.0
    for fills that should not be penalized (e.g., no_bid in a YES-only
    penalty config). Earlier versions hard-coded the yes_bid guard, but
    the directional penalty mode needs to support penalizing either side.
    """
    kept = []
    for f in fills:
        p = survival_fn(f)
        if rng.random() < p:
            kept.append(f)
    return kept


def _split_into_unit_contracts(fills):
    """Expand each fill into per-contract entries so size>1 fills pair off
    contract-by-contract (matching the engine's pair_off_inventory behavior
    where yes_queue / no_queue store one entry per contract).
    """
    units = []
    for f in fills:
        size = f["size"]
        for _ in range(size):
            units.append({
                "side": f["side"],
                "price": f["price"],
                "filled_at": f["filled_at"],
                "fill_id": f.get("id"),
                "size": 1,
            })
    return units


def simulate_session_ticker(fills, last_snap, outcome):
    """Process a single (session_id, ticker) cohort of fills.

    Args:
        fills: list of fill dicts with side ('yes_bid'|'no_bid'), price (int
            cents), size (int contracts), filled_at (ISO timestamp string).
        last_snap: dict with best_yes_bid + yes_ask, or None. Used to price
            forced exits for unpaired inventory.
        outcome: 'yes' | 'no' | None. Settlement direction; used as fallback
            when last_snap is None.

    Returns dict with:
        pair_pnl_c: total gross P&L from FIFO pair-offs (positive = profit)
        exit_pnl_c: total gross P&L from forced exits / settlement of unpaired
        yes_remaining: count of yes contracts not flattenable (no snap + no outcome)
        no_remaining: same for no
        maker_fee_c: total maker fee (negative = rebate, our credit)
        taker_fee_c: total taker fee charged on exits (positive cost)
        net_pnl_c: pair_pnl + exit_pnl - maker_fee - taker_fee
    """
    # Sort chronologically; the bot processes fills in arrival order
    sorted_fills = sorted(fills, key=lambda f: f["filled_at"])
    units = _split_into_unit_contracts(sorted_fills)

    # FIFO queues hold prices of unpaired contracts
    yes_queue: list[int] = []
    no_queue: list[int] = []
    pair_pnl = 0.0
    maker_fee = 0.0

    for u in units:
        # Each entered fill earns the maker rebate
        maker_fee += calculate_maker_fee(u["price"], "sports", count=1)
        if u["side"] == "yes_bid":
            if no_queue:
                no_cost = no_queue.pop(0)
                pair_pnl += 100 - u["price"] - no_cost
            else:
                yes_queue.append(u["price"])
        else:  # no_bid
            if yes_queue:
                yes_cost = yes_queue.pop(0)
                pair_pnl += 100 - yes_cost - u["price"]
            else:
                no_queue.append(u["price"])

    # Exit unpaired inventory. "_remaining" captures the count of unpaired
    # contracts that entered the exit phase (regardless of whether they
    # exited via BBO, via settlement, or had to be abandoned).
    exit_pnl = 0.0
    taker_fee = 0.0
    yes_remaining = len(yes_queue)
    no_remaining = len(no_queue)

    if yes_queue:
        if last_snap and last_snap.get("best_yes_bid") is not None:
            exit_price = int(last_snap["best_yes_bid"])
            for yes_cost in yes_queue:
                exit_pnl += exit_price - yes_cost
                taker_fee += _taker_fee_per_contract(exit_price)
        elif outcome is not None:
            settle_payout = 100 if outcome == "yes" else 0
            for yes_cost in yes_queue:
                exit_pnl += settle_payout - yes_cost
        # else: no snap + no outcome → abandon (exit_pnl unchanged)

    if no_queue:
        if last_snap and last_snap.get("yes_ask") is not None:
            exit_price = 100 - int(last_snap["yes_ask"])  # = best_no_bid
            for no_cost in no_queue:
                exit_pnl += exit_price - no_cost
                taker_fee += _taker_fee_per_contract(exit_price)
        elif outcome is not None:
            settle_payout = 100 if outcome == "no" else 0
            for no_cost in no_queue:
                exit_pnl += settle_payout - no_cost
        # else: no snap + no outcome → abandon

    net_pnl = pair_pnl + exit_pnl - maker_fee - taker_fee
    return {
        "pair_pnl_c": pair_pnl,
        "exit_pnl_c": exit_pnl,
        "yes_remaining": yes_remaining,
        "no_remaining": no_remaining,
        "maker_fee_c": maker_fee,
        "taker_fee_c": taker_fee,
        "net_pnl_c": net_pnl,
    }


# ---------------------------------------------------------------------------
# Data loading (DB + outcomes)
# ---------------------------------------------------------------------------

def load_fills(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT id, session_id, ticker, side, price, size, fee, filled_at
        FROM mm_fills
        WHERE side IN ('yes_bid', 'no_bid')
        ORDER BY filled_at
    """).fetchall()
    return [{"id": r[0], "session_id": r[1], "ticker": r[2], "side": r[3],
             "price": r[4], "size": r[5], "fee": r[6], "filled_at": r[7]}
            for r in rows]


def tag_offset_buckets(conn, fills):
    """Annotate each fill with `_bucket` (matching the static counterfactual)."""
    for f in fills:
        row = conn.execute("""
            SELECT best_yes_bid, yes_ask
            FROM mm_snapshots
            WHERE ticker = ? AND session_id = ?
              AND ABS(strftime('%s', ts) - strftime('%s', ?)) < 90
            ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?))
            LIMIT 1
        """, (f["ticker"], f["session_id"],
              f["filled_at"], f["filled_at"])).fetchone()
        if row is None:
            f["_bucket"] = "no_snapshot"
            continue
        best_yes_bid, yes_ask = row
        if f["side"] == "yes_bid":
            ref = best_yes_bid
        else:
            ref = (100 - yes_ask) if yes_ask is not None else None
        if ref is None:
            f["_bucket"] = "no_snapshot"
            continue
        offset = f["price"] - ref
        if offset >= 1:
            f["_bucket"] = "+1_or_above"
        elif offset == 0:
            f["_bucket"] = 0
        elif offset == -1:
            f["_bucket"] = -1
        else:  # offset <= -2: very unusual
            f["_bucket"] = "+1_or_above"


def last_snap_per_session_ticker(conn) -> dict:
    """For each (session_id, ticker), return the last snapshot's best_yes_bid + yes_ask."""
    rows = conn.execute("""
        SELECT s.session_id, s.ticker, s.best_yes_bid, s.yes_ask
        FROM mm_snapshots s
        JOIN (
          SELECT session_id, ticker, MAX(ts) AS max_ts
          FROM mm_snapshots
          GROUP BY session_id, ticker
        ) latest
          ON s.session_id = latest.session_id
         AND s.ticker = latest.ticker
         AND s.ts = latest.max_ts
    """).fetchall()
    return {(r[0], r[1]): {"best_yes_bid": r[2], "yes_ask": r[3]} for r in rows}


def load_outcomes(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        cache = json.load(f)
    outcomes = {}
    for slug, info in cache.items():
        if not isinstance(info, dict):
            continue
        for key in ("winning_outcome", "outcome", "resolution", "result"):
            v = info.get(key)
            if v:
                outcomes[slug] = "yes" if "yes" in str(v).lower() else "no"
                break
    return outcomes


# ---------------------------------------------------------------------------
# Aggregator + Monte Carlo trials
# ---------------------------------------------------------------------------

def run_trial(fills_by_st, snaps_by_st, outcomes, survival_fn, rng):
    """One Monte Carlo trial: apply survival, then sim every (session, ticker)."""
    totals = {"pair_pnl_c": 0.0, "exit_pnl_c": 0.0, "maker_fee_c": 0.0,
              "taker_fee_c": 0.0, "net_pnl_c": 0.0,
              "yes_remaining": 0, "no_remaining": 0,
              "yes_fills_kept": 0, "no_fills_kept": 0}
    for (session_id, ticker), fills in fills_by_st.items():
        kept = apply_survival_to_fills(fills, survival_fn, rng)
        for f in kept:
            if f["side"] == "yes_bid":
                totals["yes_fills_kept"] += 1
            else:
                totals["no_fills_kept"] += 1
        snap = snaps_by_st.get((session_id, ticker))
        outcome = outcomes.get(ticker)
        result = simulate_session_ticker(kept, snap, outcome)
        for k in ("pair_pnl_c", "exit_pnl_c", "maker_fee_c",
                  "taker_fee_c", "net_pnl_c", "yes_remaining", "no_remaining"):
            totals[k] += result[k]
    return totals


def make_differential_survival_fn(per_prefix_model: dict[str, str]):
    """Build a survival_fn that applies penalty only to selected prefixes.

    Args:
        per_prefix_model: maps ticker prefix (e.g., "tsc") → survival
            model name (e.g., "base"). Prefixes not in the map get no
            penalty (survival = 1.0). Validated up front: unknown model
            names raise KeyError immediately.

    Returns a function compatible with apply_survival_to_fills:
        survival_fn(fill_dict) → float in [0, 1]

    The fill_dict must have "ticker" and "_bucket" keys.
    """
    # Validate model names eagerly so callers get a clean error
    resolved = {}
    for prefix, model_name in per_prefix_model.items():
        resolved[prefix] = SURVIVAL_MODELS[model_name]  # raises KeyError if unknown

    def survival_fn(fill):
        # YES-only differential — no_bid fills always pass
        if fill.get("side") != "yes_bid":
            return 1.0
        ticker = fill.get("ticker", "")
        prefix = ticker[:3] if ticker else ""
        model = resolved.get(prefix)
        if model is None:
            return 1.0  # no penalty for this prefix
        return model.get(fill.get("_bucket"), 0.4)

    return survival_fn


def _prefix7(ticker: str) -> str:
    """Extract a 7-char-ish prefix: first two dash-delimited parts.

    'tsc-mlb-az-tex-2026-05-11-7pt5' → 'tsc-mlb'
    'aec-cs2-foo-bar-2026-05-13'     → 'aec-cs2'
    'astatc-mlb-foo-2026-05-13'      → 'astatc-mlb'
    """
    if not ticker or "-" not in ticker:
        return ticker or ""
    parts = ticker.split("-", 2)
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return ticker


def make_directional_survival_fn(penalty_map: dict[str, dict]):
    """Build a survival_fn for the auto-calibrated directional penalty.

    Args:
        penalty_map: dict mapping prefix7 (e.g., "tsc-mlb") → config dict
            with keys:
              direction: "yes_penalty" | "no_penalty" | None
              model: survival model name (default "base") - used to set
                     the magnitude of the penalty's effect
        Prefixes absent from the map get NO penalty (survival = 1.0 for
        both sides). Use this for prefixes where the trade tape signal
        is too weak / unavailable.

    Returns:
        survival_fn(fill_dict) → float in [0, 1]. The fill_dict must have
        "ticker", "side", and "_bucket" keys.
    """
    resolved = {}
    for prefix, cfg in penalty_map.items():
        direction = cfg.get("direction")
        if direction is None:
            continue
        if direction not in ("yes_penalty", "no_penalty"):
            raise ValueError(
                f"invalid direction for prefix {prefix}: {direction!r}"
            )
        model_name = cfg.get("model", "base")
        resolved[prefix] = {
            "direction": direction,
            "model": SURVIVAL_MODELS[model_name],  # raises KeyError if unknown
        }

    def survival_fn(fill):
        prefix = _prefix7(fill.get("ticker", ""))
        cfg = resolved.get(prefix)
        if cfg is None:
            return 1.0  # no penalty for this prefix
        # Apply penalty ONLY to the side flagged by the direction
        if cfg["direction"] == "yes_penalty" and fill.get("side") == "yes_bid":
            return cfg["model"].get(fill.get("_bucket"), 0.4)
        if cfg["direction"] == "no_penalty" and fill.get("side") == "no_bid":
            return cfg["model"].get(fill.get("_bucket"), 0.4)
        return 1.0  # opposite side not penalized

    return survival_fn


def compute_directional_penalty_map(
    trade_tape_db: str,
    threshold_high: float = 0.55,
    threshold_low: float = 0.45,
    min_trades: int = 30,
    model: str = "base",
) -> dict[str, dict]:
    """Derive per-prefix7 penalty direction from trade tape flow signal.

    For each prefix7 in the trade tape, compute the YES_BID share:
        share = maker_buy / (maker_buy + maker_sell)

    Direction mapping:
        share > threshold_high → 'yes_penalty' (YES_BID drain heavy →
            penalize YES bids to reduce adverse selection)
        share < threshold_low  → 'no_penalty' (YES_ASK / NO_BID drain
            heavy → penalize NO bids)
        otherwise              → None (balanced flow; no penalty)

    Args:
        trade_tape_db: path to data/poly_trade_tape.db
        threshold_high/low: dead zone is [low, high]
        min_trades: skip prefixes with fewer than this many trades
            (too noisy to be reliable)
        model: SURVIVAL_MODELS name for the penalty intensity

    Returns:
        dict[prefix7] -> {"direction": str|None, "model": str,
                          "yes_bid_share": float, "n_trades": int}
    """
    conn = sqlite3.connect(trade_tape_db)
    try:
        rows = conn.execute("""
            SELECT
                substr(market_slug, 1, instr(market_slug || '-',
                    '-', instr(market_slug, '-')+1) - 1) AS prefix7,
                maker_side,
                COUNT(*) AS n
            FROM mm_trade_tape
            GROUP BY prefix7, maker_side
        """).fetchall()
    except sqlite3.OperationalError:
        # Fallback: simpler SQL if SQLite version doesn't support that nested instr
        rows = conn.execute("""
            SELECT market_slug, maker_side FROM mm_trade_tape
        """).fetchall()
        # Manual aggregation
        from collections import defaultdict
        agg: dict = defaultdict(lambda: defaultdict(int))
        for slug, ms in rows:
            agg[_prefix7(slug)][ms] += 1
        rows = []
        for p, sides in agg.items():
            for ms, n in sides.items():
                rows.append((p, ms, n))

    by_prefix: dict[str, dict] = {}
    for p, ms, n in rows:
        if p not in by_prefix:
            by_prefix[p] = {"buy": 0, "sell": 0}
        if ms == "ORDER_SIDE_BUY":
            by_prefix[p]["buy"] += n
        elif ms == "ORDER_SIDE_SELL":
            by_prefix[p]["sell"] += n
    conn.close()

    result: dict[str, dict] = {}
    for prefix, counts in by_prefix.items():
        total = counts["buy"] + counts["sell"]
        if total < min_trades:
            continue
        share = counts["buy"] / total
        if share > threshold_high:
            direction = "yes_penalty"
        elif share < threshold_low:
            direction = "no_penalty"
        else:
            direction = None
        result[prefix] = {
            "direction": direction,
            "model": model,
            "yes_bid_share": share,
            "n_trades": total,
        }
    return result


def run_simulation(fills_by_st, snaps_by_st, outcomes, survival_model_name,
                   n_trials=200, seed=0, per_prefix_model=None,
                   directional_penalty_map=None):
    """Run n_trials Monte Carlo trials; return mean + percentile summary.

    Precedence (highest to lowest):
        1. directional_penalty_map (per-prefix7 YES_or_NO penalty)
        2. per_prefix_model (per-prefix3 YES-only penalty, differential)
        3. survival_model_name (uniform YES penalty model)
        4. None → baseline (no penalty)

    Args:
        survival_model_name: None (baseline), or "pessimistic"/"base"/
            "optimistic". Ignored if higher-precedence args set.
        per_prefix_model: dict prefix3 → model name for YES-only
            differential (e.g., {"tsc": "base"}).
        directional_penalty_map: dict prefix7 → {"direction": "yes_penalty"
            | "no_penalty" | None, "model": str}. Built from trade tape
            via compute_directional_penalty_map().
    """
    if directional_penalty_map is not None:
        survival_fn = make_directional_survival_fn(directional_penalty_map)
    elif per_prefix_model is not None:
        survival_fn = make_differential_survival_fn(per_prefix_model)
    elif survival_model_name is None:
        survival_fn = lambda f: 1.0  # no penalty (baseline)
        # Baseline is deterministic; one trial suffices
        n_trials = 1
    else:
        model = SURVIVAL_MODELS[survival_model_name]
        # YES-only by default; no_bid fills always survive
        survival_fn = lambda f: (
            model.get(f.get("_bucket"), 0.4) if f.get("side") == "yes_bid"
            else 1.0
        )

    rng = random.Random(seed)
    trials = [run_trial(fills_by_st, snaps_by_st, outcomes, survival_fn, rng)
              for _ in range(n_trials)]

    keys = ("pair_pnl_c", "exit_pnl_c", "maker_fee_c", "taker_fee_c",
            "net_pnl_c", "yes_fills_kept", "no_fills_kept")
    summary = {}
    for k in keys:
        vals = [t[k] for t in trials]
        summary[k + "_mean"] = statistics.mean(vals)
        if n_trials > 1:
            summary[k + "_stdev"] = statistics.stdev(vals)
            summary[k + "_p25"] = statistics.quantiles(vals, n=4)[0]
            summary[k + "_p75"] = statistics.quantiles(vals, n=4)[2]
        else:
            summary[k + "_stdev"] = 0.0
            summary[k + "_p25"] = vals[0]
            summary[k + "_p75"] = vals[0]
    summary["n_trials"] = n_trials
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def slice_fills_by_prefix(fills_by_st, prefix):
    return {(s, t): fs for (s, t), fs in fills_by_st.items()
            if t.startswith(prefix + "-")}


def parse_differential_spec(spec: str) -> dict[str, str]:
    """Parse a CLI spec like 'tsc:base,asc:pessimistic' → dict."""
    out = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"differential spec entry missing ':': {chunk}")
        prefix, model = chunk.split(":", 1)
        out[prefix.strip()] = model.strip()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--filter-prefix", default=None,
                        help="Only run simulator on slugs starting with this prefix")
    parser.add_argument("--differential", default=None,
                        help="Differential penalty spec, e.g. 'tsc:base' to apply "
                             "the base survival model only to tsc fills. Comma-"
                             "separate multiple: 'tsc:base,asc:pessimistic'. "
                             "When set, the standard per-model table is replaced "
                             "with a single 'differential' row per slice.")
    parser.add_argument("--auto-calibrate", action="store_true",
                        help="Build a directional penalty map from the trade "
                             "tape (data/poly_trade_tape.db) and run the "
                             "simulator with that config. Each prefix is "
                             "flagged 'yes_penalty', 'no_penalty', or none "
                             "based on observed flow direction.")
    parser.add_argument("--trade-tape-db",
                        default="/Users/openclaw/polymarket-arb/data/poly_trade_tape.db",
                        help="Path to trade tape DB for auto-calibration")
    parser.add_argument("--auto-threshold-high", type=float, default=0.55,
                        help="YES_BID share above which a prefix gets "
                             "yes_penalty (default 0.55)")
    parser.add_argument("--auto-threshold-low", type=float, default=0.45,
                        help="YES_BID share below which a prefix gets "
                             "no_penalty (default 0.45)")
    parser.add_argument("--auto-min-trades", type=int, default=30,
                        help="Minimum trades to include a prefix in the "
                             "auto-calibration (default 30)")
    parser.add_argument("--auto-model", default="base",
                        choices=list(SURVIVAL_MODELS.keys()),
                        help="Survival model magnitude for auto-calibration "
                             "(default base)")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    fills = load_fills(conn)
    print(f"Loaded {len(fills)} fills from {args.db}")

    tag_offset_buckets(conn, fills)
    snaps_by_st = last_snap_per_session_ticker(conn)
    outcomes = load_outcomes(SETTLEMENT_CACHE)
    print(f"Loaded snapshots for {len(snaps_by_st)} (session, ticker) cohorts")
    print(f"Loaded outcomes for {len(outcomes)} resolved tickers")

    # Group fills by (session_id, ticker)
    fills_by_st = defaultdict(list)
    for f in fills:
        fills_by_st[(f["session_id"], f["ticker"])].append(f)

    print(f"\nUnique (session, ticker) cohorts: {len(fills_by_st)}")

    slices = [("ALL", fills_by_st)]
    if args.filter_prefix:
        slices = [(args.filter_prefix, slice_fills_by_prefix(fills_by_st, args.filter_prefix))]
    else:
        for prefix in ("aec", "asc", "atc", "tsc"):
            slices.append((prefix, slice_fills_by_prefix(fills_by_st, prefix)))

    print("\n" + "=" * 80)
    print(f"{'Slice':<8s}  {'Model':<13s}  {'Trials':>7s}  "
          f"{'Net P&L $':>11s}  {'p25..p75':>17s}  {'YES kept':>9s}  {'NO kept':>9s}")
    print("=" * 80)
    # Build configs list: each config is (model_name, label, prefix_map,
    # directional_map). One of prefix_map or directional_map is non-None
    # (or both None for baseline / uniform survival models).
    if args.auto_calibrate:
        directional_map = compute_directional_penalty_map(
            args.trade_tape_db,
            threshold_high=args.auto_threshold_high,
            threshold_low=args.auto_threshold_low,
            min_trades=args.auto_min_trades,
            model=args.auto_model,
        )
        print(f"\nAuto-calibration from trade tape ({args.trade_tape_db}):")
        print(f"  thresholds: high={args.auto_threshold_high} "
              f"low={args.auto_threshold_low}  min_trades={args.auto_min_trades}")
        print(f"  prefixes calibrated: {len(directional_map)}")
        for p in sorted(directional_map.keys()):
            cfg = directional_map[p]
            dir_str = cfg["direction"] or "none (balanced)"
            print(f"    {p:<14s}  share={cfg['yes_bid_share']*100:>5.1f}%  "
                  f"n={cfg['n_trades']:>4d}  → {dir_str}")
        spec_label = f"auto[{args.auto_model},n={args.auto_min_trades}]"
        configs = [(None, "baseline", None, None),
                   (None, spec_label, None, directional_map)]
    elif args.differential:
        differential_map = parse_differential_spec(args.differential)
        spec_label = "diff[" + ",".join(f"{k}={v}" for k, v in differential_map.items()) + "]"
        configs = [(None, "baseline", None, None),
                   (None, spec_label, differential_map, None)]
    else:
        configs = [(None, "baseline", None, None),
                   ("pessimistic", "pessimistic", None, None),
                   ("base", "base", None, None),
                   ("optimistic", "optimistic", None, None)]

    rows = []
    for slice_label, slice_fills in slices:
        for model_name, label, prefix_map, dir_map in configs:
            is_baseline = (model_name is None and prefix_map is None
                           and dir_map is None)
            n_trials = 1 if is_baseline else args.trials
            summary = run_simulation(
                slice_fills, snaps_by_st, outcomes,
                model_name, n_trials=n_trials, seed=args.seed,
                per_prefix_model=prefix_map,
                directional_penalty_map=dir_map,
            )
            net_mean_dollars = summary["net_pnl_c_mean"] / 100
            p25 = summary["net_pnl_c_p25"] / 100
            p75 = summary["net_pnl_c_p75"] / 100
            yes_kept = summary["yes_fills_kept_mean"]
            no_kept = summary["no_fills_kept_mean"]
            rows.append({
                "slice": slice_label, "model": label,
                "net_mean_c": summary["net_pnl_c_mean"],
                "net_p25_c": summary["net_pnl_c_p25"],
                "net_p75_c": summary["net_pnl_c_p75"],
                "pair_mean_c": summary["pair_pnl_c_mean"],
                "exit_mean_c": summary["exit_pnl_c_mean"],
                "maker_fee_mean_c": summary["maker_fee_c_mean"],
                "taker_fee_mean_c": summary["taker_fee_c_mean"],
                "yes_kept": yes_kept,
                "no_kept": no_kept,
                "n_trials": summary["n_trials"],
            })
            print(f"{slice_label:<8s}  {label:<13s}  {n_trials:>7d}  "
                  f"${net_mean_dollars:>+9.2f}   "
                  f"[${p25:>+5.2f}, ${p75:>+5.2f}]  "
                  f"{yes_kept:>9.1f}  {no_kept:>9.1f}")

    # Decomposition: pair + exit - maker_fee - taker_fee per slice × model
    print("\n" + "=" * 80)
    print("Decomposition per slice × model: pair + exit - maker_fee - taker_fee")
    print("=" * 80)
    print(f"{'Slice':<6s}  {'Model':<13s}  {'Pair':>9s}  {'Exit':>9s}  "
          f"{'Maker':>9s}  {'Taker':>9s}  {'Net $':>9s}")
    last_slice = None
    for row in rows:
        if last_slice is not None and row["slice"] != last_slice:
            print()  # blank line between slices
        last_slice = row["slice"]
        print(f"{row['slice']:<6s}  {row['model']:<13s}  "
              f"${row['pair_mean_c']/100:>+7.2f}  "
              f"${row['exit_mean_c']/100:>+7.2f}  "
              f"${row['maker_fee_mean_c']/100:>+7.2f}  "
              f"${row['taker_fee_mean_c']/100:>+7.2f}  "
              f"${row['net_mean_c']/100:>+7.2f}")

    conn.close()
    return rows


if __name__ == "__main__":
    main()
