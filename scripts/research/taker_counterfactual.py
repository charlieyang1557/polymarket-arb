#!/usr/bin/env python3
"""Path B Option 3 — taker counterfactual on 326 historical fills.

For each historical maker fill, compute what P&L would have been if we'd
been the TAKER on the OPPOSITE side instead. The thesis (from
path_b_options.md): the flow we're being adversely-selected on as a
maker IS captureable as a taker. If the historical maker hold-to-settle
P&L is significantly negative, flipping every fill to taker (joining
the historical taker's direction) should be net positive after taker
fees.

Flip semantics:
  Maker yes_bid at price P (bot acquired YES at P):
    → Taker SELLS YES at P (joins the YES-seller taker that hit us).
    → Bot is short YES at P; at settle:
        outcome YES → owes 100, received P → P&L = P - 100
        outcome NO  → owes 0,  received P → P&L = +P
    → Taker fee: 0.02 * P * (1-P) * 100 per contract (positive cost)

  Maker no_bid at price P (bot acquired NO at P):
    → Taker SELLS NO at P (joins the NO-seller taker).
    → Bot is short NO at P; at settle:
        outcome NO  → owes 100 → P&L = P - 100
        outcome YES → owes 0   → P&L = +P
    → Taker fee: same formula (sports category)

Then aggregate:
  taker_settled_pnl_c = sum over fills of (flip_pnl_per_contract * size)
  taker_fees_c        = sum over fills of (taker_fee_cents(price) * size)
  taker_net_pnl_c     = taker_settled_pnl_c - taker_fees_c

Compare to historical maker hold-to-settle:
  maker_net_pnl_c (per yes_penalty_counterfactual.py) = -$13.49

Three possible outcomes:
  - taker_net > 0: Option 3 thesis holds; flow is captureable as taker.
  - taker_net < 0: OBI fair-value model is wrong; bot's edge thesis
    breaks down regardless of role (maker or taker).
  - taker_net ≈ 0: friction (fees) eats the directional edge; consider
    Option 4 (single-sided MM) instead.

Run:
  python scripts/research/taker_counterfactual.py
  python scripts/research/taker_counterfactual.py --filter-prefix tsc-nba
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = "/Users/openclaw/polymarket-arb/data/poly_mm_live.db"
SETTLEMENT_CACHE = (
    "/Users/openclaw/polymarket-arb/.claude/worktrees/"
    "charming-mcclintock-caff3f/data/research/resolved_markets_cache.json"
)


# ---------------------------------------------------------------------------
# Pure functions (tested in tests/test_taker_counterfactual.py)
# ---------------------------------------------------------------------------

def taker_fee_cents(price_cents: int, size: int) -> float:
    """Polymarket sports taker fee per contract × size.

    Formula: 0.02 * P * (1-P) * 100 * size, where P = price_cents / 100.
    Maximum at P=0.5: 0.5c per contract.
    """
    p = price_cents / 100
    return 0.02 * p * (1 - p) * 100 * size


def flip_fill_to_taker_pnl_settled(fill: dict, outcome: str | None) -> float:
    """Per-fill settlement P&L for the taker flip (no fee).

    For yes_bid maker fill at price P:
      Taker SELLS YES at P. Short YES.
        outcome 'yes': owes 100, received P → P&L = (P - 100) * size
        outcome 'no':  owes 0,   received P → P&L = +P * size

    For no_bid maker fill at price P:
      Taker SELLS NO at P. Short NO.
        outcome 'no':  owes 100 → P&L = (P - 100) * size
        outcome 'yes': owes 0   → P&L = +P * size

    Returns 0 if outcome is None (unresolved or missing from cache).
    """
    if outcome is None:
        return 0.0
    price = fill["price"]
    size = fill["size"]
    if fill["side"] == "yes_bid":
        per_contract = (price - 100) if outcome == "yes" else price
    elif fill["side"] == "no_bid":
        per_contract = (price - 100) if outcome == "no" else price
    else:
        return 0.0
    return per_contract * size


def prefix7(ticker: str) -> str:
    """First two dash-delimited components: 'tsc-mlb', 'aec-nhl', etc."""
    if not ticker or "-" not in ticker:
        return ticker or ""
    parts = ticker.split("-", 2)
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return ticker


def aggregate_per_prefix(fills, outcomes) -> dict:
    """Sum taker counterfactual P&L per prefix7."""
    agg: dict = defaultdict(lambda: {
        "n_fills": 0, "n_fills_skipped": 0, "n_contracts": 0,
        "settled_pnl_c": 0.0, "taker_fees_c": 0.0, "net_pnl_c": 0.0,
        "by_side": defaultdict(lambda: {
            "n_fills": 0, "n_contracts": 0, "settled_pnl_c": 0.0,
        }),
    })
    for f in fills:
        p7 = prefix7(f["ticker"])
        b = agg[p7]
        b["n_fills"] += 1
        b["n_contracts"] += f["size"]
        outcome = outcomes.get(f["ticker"])
        if outcome is None:
            b["n_fills_skipped"] += 1
            continue
        s = flip_fill_to_taker_pnl_settled(f, outcome)
        fee = taker_fee_cents(f["price"], f["size"])
        b["settled_pnl_c"] += s
        b["taker_fees_c"] += fee
        b["net_pnl_c"] += (s - fee)
        sd = b["by_side"][f["side"]]
        sd["n_fills"] += 1
        sd["n_contracts"] += f["size"]
        sd["settled_pnl_c"] += s
    return dict(agg)


# ---------------------------------------------------------------------------
# Data loading
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
# Report
# ---------------------------------------------------------------------------

def print_report(per_prefix: dict, args):
    """Pretty-print the per-prefix aggregate."""
    # Compute also the MAKER comparison (hold-to-settle, same as
    # yes_penalty_counterfactual baseline) so user can see both side by side.
    rows = sorted(per_prefix.items(), key=lambda kv: -kv[1]["n_contracts"])
    total_settled = sum(v["settled_pnl_c"] for v in per_prefix.values())
    total_fees = sum(v["taker_fees_c"] for v in per_prefix.values())
    total_net = sum(v["net_pnl_c"] for v in per_prefix.values())
    total_fills = sum(v["n_fills"] for v in per_prefix.values())
    total_skipped = sum(v["n_fills_skipped"] for v in per_prefix.values())
    total_contracts = sum(v["n_contracts"] for v in per_prefix.values())

    print()
    print("=" * 90)
    print("TAKER COUNTERFACTUAL — flip every historical maker fill to a taker on the opposite side")
    print("=" * 90)
    print(f"{'prefix7':<14s} {'fills':>6s} {'cntrx':>6s} "
          f"{'settled $':>10s} {'fees $':>9s} {'net $':>10s}   "
          f"{'yes fills':>10s} {'no fills':>10s}")
    print("-" * 90)
    for p, v in rows:
        if v["n_fills"] == 0:
            continue
        yes_fills = v["by_side"].get("yes_bid", {"n_fills": 0})["n_fills"]
        no_fills = v["by_side"].get("no_bid", {"n_fills": 0})["n_fills"]
        print(f"{p:<14s} {v['n_fills']:>6d} {v['n_contracts']:>6d} "
              f"${v['settled_pnl_c']/100:>+8.2f} "
              f"${v['taker_fees_c']/100:>+7.2f} "
              f"${v['net_pnl_c']/100:>+8.2f}   "
              f"{yes_fills:>10d} {no_fills:>10d}")
    print("-" * 90)
    print(f"{'TOTAL':<14s} {total_fills:>6d} {total_contracts:>6d} "
          f"${total_settled/100:>+8.2f} "
          f"${total_fees/100:>+7.2f} "
          f"${total_net/100:>+8.2f}")
    print()
    print(f"Total fills with settlement data:    {total_fills - total_skipped}")
    print(f"Total fills skipped (no settlement): {total_skipped}")
    print()
    print("Compare to baseline MAKER hold-to-settle (from yes_penalty_counterfactual.py):")
    print(f"  Maker net hold-to-settle: -$12.69 (yes -$18.85 + no +$5.36 + rebate +$0.80)")
    print(f"  Taker net hold-to-settle: ${total_net/100:+.2f}")
    print(f"  Delta: ${(total_net/100) - (-12.69):+.2f}")
    print()
    print("Interpretation:")
    if total_net > 100:  # >$1
        print("  TAKER counterfactual is significantly POSITIVE.")
        print("  → Option 3 thesis holds: flow we were adversely-selected on")
        print("    IS captureable as a taker, even after fees.")
        print("  → Caveat: hold-to-settle has settlement-direction risk per trade,")
        print("    and assumes we'd have taken on every flip opportunity.")
        print("  → Next: design forward observer that watches trade tape for")
        print("    high-conviction flip opportunities and logs counterfactual.")
    elif total_net < -100:
        print("  TAKER counterfactual is significantly NEGATIVE.")
        print("  → The bot's OBI fair-value model itself may be biased.")
        print("  → If maker AND taker both lose hold-to-settle, the 'edge' the")
        print("    bot was modeling doesn't exist on these markets.")
        print("  → Investigate: per-market mid-vs-settled bias.")
    else:
        print("  TAKER counterfactual is NEAR ZERO after fees.")
        print("  → Friction eats the directional edge.")
        print("  → Consider Option 4 (single-sided MM) which avoids taker fees")
        print("    by passively quoting only the under-hit side.")


def _load_trade_tape_prefix_signals(trade_tape_db: str) -> dict:
    """Compute per-prefix7 YES_BID share from trade tape. Returns
    dict[prefix7] -> {"yes_bid_share": float, "n_trades": int}.
    """
    if not os.path.exists(trade_tape_db):
        return {}
    conn = sqlite3.connect(trade_tape_db)
    rows = conn.execute(
        "SELECT market_slug, maker_side FROM mm_trade_tape"
    ).fetchall()
    conn.close()
    agg = defaultdict(lambda: {"buy": 0, "sell": 0})
    for slug, ms in rows:
        p = prefix7(slug)
        if ms == "ORDER_SIDE_BUY":
            agg[p]["buy"] += 1
        elif ms == "ORDER_SIDE_SELL":
            agg[p]["sell"] += 1
    out = {}
    for p, c in agg.items():
        total = c["buy"] + c["sell"]
        if total > 0:
            out[p] = {"yes_bid_share": c["buy"] / total, "n_trades": total}
    return out


def filter_fills_by_trade_tape_signal(fills, prefix_signals: dict,
                                       threshold_high: float = 0.55,
                                       threshold_low: float = 0.45,
                                       min_trades: int = 30) -> list:
    """Keep only fills where the taker flip direction aligns with current
    trade tape signal for that prefix.

    For a yes_bid maker fill, the taker flip is SELL YES. This aligns with
    trade tape signal if YES_BID share > threshold_high (selling YES is
    the dominant taker direction in current data).

    For a no_bid maker fill, the taker flip is SELL NO (equivalent to
    BUY YES). This aligns if YES_BID share < threshold_low (taker buying
    YES is dominant).

    Prefixes with no trade tape data or balanced signal are excluded
    (no conviction).
    """
    kept = []
    for f in fills:
        p = prefix7(f["ticker"])
        sig = prefix_signals.get(p)
        if sig is None or sig["n_trades"] < min_trades:
            continue
        share = sig["yes_bid_share"]
        if f["side"] == "yes_bid" and share > threshold_high:
            kept.append(f)
        elif f["side"] == "no_bid" and share < threshold_low:
            kept.append(f)
    return kept


def time_split_fills(fills, split_ratio: float = 0.5) -> tuple[list, list]:
    """Split fills chronologically into early/late halves for stability test."""
    sorted_fills = sorted(fills, key=lambda f: f.get("filled_at", ""))
    split_idx = int(len(sorted_fills) * split_ratio)
    return sorted_fills[:split_idx], sorted_fills[split_idx:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--filter-prefix", default=None,
                        help="Restrict to fills whose ticker starts with this prefix7")
    parser.add_argument("--trade-tape-db",
                        default="/Users/openclaw/polymarket-arb/data/poly_trade_tape.db",
                        help="Path to trade tape DB for signal-validated flip")
    parser.add_argument("--validate-with-tape", action="store_true",
                        help="Only count flips where trade tape signal aligns")
    parser.add_argument("--time-split", action="store_true",
                        help="Run separately on early/late halves of fills (stability test)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    fills = load_fills(conn)
    print(f"Loaded {len(fills)} fills from {args.db}")
    conn.close()

    if args.filter_prefix:
        fills = [f for f in fills if prefix7(f["ticker"]) == args.filter_prefix]
        print(f"Filtered to {len(fills)} fills matching prefix7={args.filter_prefix}")

    outcomes = load_outcomes(SETTLEMENT_CACHE)
    print(f"Loaded {len(outcomes)} resolved outcomes")

    if args.validate_with_tape:
        signals = _load_trade_tape_prefix_signals(args.trade_tape_db)
        print(f"\nTrade tape prefix signals (from {args.trade_tape_db}):")
        for p, sig in sorted(signals.items()):
            print(f"  {p:<14s}  share={sig['yes_bid_share']*100:>5.1f}%  n={sig['n_trades']:>4d}")
        kept = filter_fills_by_trade_tape_signal(fills, signals)
        print(f"\nValidated subset: {len(kept)}/{len(fills)} fills where flip "
              f"direction matches current trade tape signal")
        print("(Skipped fills are where current signal contradicts or is too weak)")
        fills = kept

    if args.time_split:
        early, late = time_split_fills(fills)
        print(f"\n{'=' * 60}\nEARLY HALF (oldest {len(early)} fills)\n{'=' * 60}")
        print_report(aggregate_per_prefix(early, outcomes), args)
        print(f"\n{'=' * 60}\nLATE HALF (newest {len(late)} fills)\n{'=' * 60}")
        print_report(aggregate_per_prefix(late, outcomes), args)
    else:
        agg = aggregate_per_prefix(fills, outcomes)
        print_report(agg, args)


if __name__ == "__main__":
    main()
