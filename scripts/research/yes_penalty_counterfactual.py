#!/usr/bin/env python3
"""Counterfactual: what would the 1c YES adverse-selection penalty have
done to the 326 real live fills in poly_mm_live.db?

For each yes_bid fill, find the nearest snapshot (within ±60s of fill
time) to determine our bid's offset from best_yes_bid. Apply a fill
survival probability model based on that offset:

  offset = 0  (our bid AT BBO): under penalty we drop 1c, sitting
              BELOW BBO. Survival depends on whether BBO collapsed
              far enough during the fill window to clear us.
  offset = -1 (our bid 1c BELOW BBO, i.e., BBO collapsed onto our
              price during the fill window): under penalty we're now
              2c below original BBO. Survival is lower.
  offset = +1 (our bid 1c ABOVE BBO): rare; in those cases the BBO
              was below us when the snapshot was taken. With penalty
              we'd be AT BBO, so survival is high.

Three survival models — pessimistic / base / optimistic — give a range.

The script reports:
  - Counterfactual yes:no fill ratio
  - Counterfactual hold-to-settle P&L (using settlement_pnl_summary.json
    per-ticker means)
  - Sensitivity to the survival model

Limitations:
  - Snapshot timing is up to 60s stale. The exact orderbook state at
    fill is unknown; this is a probabilistic estimate.
  - Doesn't model the dynamic effect (penalty changes WHICH markets we
    target; the scanner-fix dimension is separate).
  - Assumes no_bid fills are unaffected by the penalty (only YES is
    penalized).
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = "/Users/openclaw/polymarket-arb/data/poly_mm_live.db"
SETTLEMENT_JSON = ("/Users/openclaw/polymarket-arb/.claude/worktrees/"
                   "charming-mcclintock-caff3f/data/research/"
                   "settlement_pnl_summary.json")

# Survival probability models. Each maps "offset bucket" to P(fill survives
# the 1c YES penalty). Pessimistic: assumes takers stop at the original
# BBO and don't continue to lower prices; optimistic: BBO collapses
# frequently far past our bid.
SURVIVAL_MODELS = {
    "pessimistic": {0: 0.20, -1: 0.10, "+1_or_above": 0.50, "no_snapshot": 0.30},
    "base":        {0: 0.40, -1: 0.20, "+1_or_above": 0.80, "no_snapshot": 0.40},
    "optimistic":  {0: 0.60, -1: 0.40, "+1_or_above": 1.00, "no_snapshot": 0.60},
}


def load_fills(conn):
    """Return list of fill dicts for yes_bid + no_bid only."""
    rows = conn.execute("""
        SELECT id, session_id, ticker, side, price, size, fee, filled_at
        FROM mm_fills
        WHERE side IN ('yes_bid', 'no_bid')
        ORDER BY filled_at
    """).fetchall()
    return [{"id": r[0], "session_id": r[1], "ticker": r[2], "side": r[3],
             "price": r[4], "size": r[5], "fee": r[6], "filled_at": r[7]}
            for r in rows]


def find_nearest_snapshot(conn, fill):
    """Within ±90s of fill, return the snapshot with closest ts."""
    row = conn.execute("""
        SELECT best_yes_bid, yes_ask, midpoint
        FROM mm_snapshots
        WHERE ticker = ? AND session_id = ?
          AND ABS(strftime('%s', ts) - strftime('%s', ?)) < 90
        ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?))
        LIMIT 1
    """, (fill["ticker"], fill["session_id"],
          fill["filled_at"], fill["filled_at"])).fetchone()
    if not row:
        return None
    return {"best_yes_bid": row[0], "yes_ask": row[1], "midpoint": row[2]}


def offset_bucket(fill, snap):
    """Compute fill price offset relative to best_yes_bid at fill time.

    For yes_bid fills, offset = fill_price - best_yes_bid.
    For no_bid fills, offset = fill_price - best_no_bid where
      best_no_bid = 100 - yes_ask.

    Returns the bucket key matching SURVIVAL_MODELS, plus the raw offset.
    """
    if snap is None:
        return "no_snapshot", None
    if fill["side"] == "yes_bid":
        ref = snap["best_yes_bid"]
    else:
        ref = 100 - (snap["yes_ask"] or 100)
    if ref is None:
        return "no_snapshot", None
    offset = fill["price"] - ref
    if offset >= 1:
        return "+1_or_above", offset
    if offset == 0:
        return 0, 0
    if offset == -1:
        return -1, -1
    # offset <= -2: very unusual (BBO was much higher than our fill)
    return "+1_or_above", offset  # treat as "would have filled anyway"


def settlement_pnl_per_fill(fill, settle):
    """Settlement P&L for this fill (hold-to-settle).

    settle: dict mapping ticker -> outcome ('yes'|'no'|None) and price/size info.
    Returns cents (positive = profit) per contract.
    """
    outcome = settle.get(fill["ticker"], {}).get("outcome")
    if outcome is None:
        return 0.0
    if fill["side"] == "yes_bid":
        # We're long YES at fill price. Settle: YES=100 (win), NO=0 (loss).
        return (100 - fill["price"]) if outcome == "yes" else (-fill["price"])
    else:
        return (100 - fill["price"]) if outcome == "no" else (-fill["price"])


def load_outcomes_from_summary(path):
    """settlement_pnl_summary has by_ticker mean_pnl, but we want per-fill
    via direct calc. Easier: reconstruct outcomes from sign of mean_pnl
    relative to side composition. Simpler: load from per-ticker totals.

    Strategy here: use the by_ticker entries' wins/losses/total_pnl/n_contracts
    to back out the outcome for each ticker by aligning fills.

    Returns dict[ticker] -> 'yes' or 'no' (the settlement outcome).
    """
    with open(path) as f:
        data = json.load(f)
    outcomes = {}
    for ticker, info in data.get("by_ticker", {}).items():
        # When all fills settled with positive P&L for yes_bid side and
        # mean_pnl > 0, the market settled in a way that favored our
        # holdings — but the sign also depends on fill composition.
        # We'll instead query the DB directly for outcomes via the SDK
        # cache (resolved_markets_cache.json).
        pass
    # Fall back to resolved cache
    cache_path = Path(path).parent / "resolved_markets_cache.json"
    if not cache_path.exists():
        return {}
    with open(cache_path) as f:
        cache = json.load(f)
    # cache is a dict of slug -> market info; "winning_outcome" or similar
    for slug, info in cache.items():
        if isinstance(info, dict):
            # Try common keys
            for key in ("winning_outcome", "outcome", "resolution", "result"):
                v = info.get(key)
                if v:
                    outcomes[slug] = "yes" if "yes" in str(v).lower() else "no"
                    break
    return outcomes


def analyze_slice(fills, outcomes, label):
    """Print baseline + per-survival-model counterfactual for one slice of fills.

    Returns dict[model_name] -> {"yes": float, "no": int, "ratio": float, "pnl_c": float}
    so callers can build a comparison table.
    """
    baseline_yes = sum(1 for f in fills if f["side"] == "yes_bid")
    baseline_no = sum(1 for f in fills if f["side"] == "no_bid")
    baseline_ratio = baseline_yes / baseline_no if baseline_no else float("inf")

    # Baseline hold-to-settle P&L (no penalty applied)
    baseline_yes_pnl = 0.0
    baseline_no_pnl = 0.0
    for f in fills:
        outcome = outcomes.get(f["ticker"])
        if not outcome:
            continue
        if f["side"] == "yes_bid":
            pnl = (100 - f["price"]) if outcome == "yes" else -f["price"]
            baseline_yes_pnl += pnl * f["size"]
        else:
            pnl = (100 - f["price"]) if outcome == "no" else -f["price"]
            baseline_no_pnl += pnl * f["size"]
    baseline_total_pnl = baseline_yes_pnl + baseline_no_pnl

    print(f"\n{'=' * 60}")
    print(f"SLICE: {label}  (n={len(fills)}: yes={baseline_yes}, no={baseline_no})")
    print(f"{'=' * 60}")
    print(f"  Baseline (no penalty): yes={baseline_yes}, no={baseline_no}, "
          f"ratio={baseline_ratio:.2f}")
    print(f"  Baseline hold-to-settle P&L: YES={baseline_yes_pnl:.0f}c, "
          f"NO={baseline_no_pnl:.0f}c, TOTAL={baseline_total_pnl:.0f}c "
          f"(${baseline_total_pnl/100:+.2f})")

    if baseline_yes == 0:
        print(f"  (skipping survival models — no yes_bid fills in slice)")
        return {"_baseline": {"ratio": baseline_ratio, "pnl_c": baseline_total_pnl,
                              "yes": baseline_yes, "no": baseline_no}}

    results = {}
    for model_name, model in SURVIVAL_MODELS.items():
        expected_yes_count = 0.0
        expected_yes_contracts = 0.0
        expected_yes_pnl = 0.0

        for f in fills:
            if f["side"] != "yes_bid":
                continue
            p_survive = model.get(f["_bucket"], 0.4)
            expected_yes_count += p_survive
            expected_yes_contracts += p_survive * f["size"]
            outcome = outcomes.get(f["ticker"])
            if outcome:
                pnl = (100 - f["price"]) if outcome == "yes" else -f["price"]
                expected_yes_pnl += p_survive * pnl * f["size"]

        no_count = baseline_no
        no_contracts = sum(f["size"] for f in fills if f["side"] == "no_bid")
        no_pnl = 0.0
        for f in fills:
            if f["side"] != "no_bid":
                continue
            outcome = outcomes.get(f["ticker"])
            if outcome:
                pnl = (100 - f["price"]) if outcome == "no" else -f["price"]
                no_pnl += pnl * f["size"]

        ratio = expected_yes_count / no_count if no_count else float("inf")
        results[model_name] = {
            "yes": expected_yes_count,
            "no": no_count,
            "ratio": ratio,
            "pnl_c": expected_yes_pnl + no_pnl,
            "yes_drop_pct": (1 - expected_yes_count / baseline_yes) * 100,
            "delta_vs_baseline": (expected_yes_pnl + no_pnl) - baseline_total_pnl,
        }
        delta = (expected_yes_pnl + no_pnl) - baseline_total_pnl
        print(f"\n  [{model_name}]")
        print(f"    Expected yes_bid fills:   {expected_yes_count:.1f} "
              f"(from {baseline_yes}, drop {(1 - expected_yes_count/baseline_yes)*100:.0f}%)")
        print(f"    Expected yes_bid contracts: {expected_yes_contracts:.1f}")
        print(f"    no_bid fills (unchanged):  {no_count}")
        print(f"    no_bid contracts:           {no_contracts}")
        print(f"    yes:no ratio (post-penalty): {ratio:.2f}")
        print(f"    YES hold-to-settle P&L:    {expected_yes_pnl:.0f}c")
        print(f"    NO hold-to-settle P&L:     {no_pnl:.0f}c")
        print(f"    Total hold-to-settle P&L:  {expected_yes_pnl + no_pnl:.0f}c "
              f"(${(expected_yes_pnl + no_pnl)/100:+.2f})")
        print(f"    DELTA vs baseline:         {delta:+.0f}c "
              f"(${delta/100:+.2f})  [+ = penalty helps]")
    # store baseline summary for the comparison table
    results["_baseline"] = {
        "ratio": baseline_ratio,
        "pnl_c": baseline_total_pnl,
        "yes": baseline_yes,
        "no": baseline_no,
    }
    return results


def main():
    conn = sqlite3.connect(DB_PATH)
    fills = load_fills(conn)
    print(f"Loaded {len(fills)} fills from {DB_PATH}")

    # Tag each fill with offset bucket (done once across the full dataset)
    bucket_counts_by_side = defaultdict(lambda: defaultdict(int))
    for f in fills:
        snap = find_nearest_snapshot(conn, f)
        bucket, raw = offset_bucket(f, snap)
        f["_bucket"] = bucket
        f["_offset_raw"] = raw
        f["_prefix"] = f["ticker"][:3]
        bucket_counts_by_side[f["side"]][bucket] += 1

    print("\nFill offset distribution (relative to BBO at fill time):")
    for side in ("yes_bid", "no_bid"):
        total = sum(bucket_counts_by_side[side].values())
        print(f"  {side} (n={total}):")
        for k, n in sorted(bucket_counts_by_side[side].items(),
                           key=lambda x: str(x[0])):
            pct = n / total * 100 if total else 0
            print(f"    {k!s:>15s}: {n:>3d} ({pct:.1f}%)")

    outcomes = load_outcomes_from_summary(SETTLEMENT_JSON)
    print(f"\nLoaded {len(outcomes)} resolved outcomes from cache "
          f"({len(outcomes) / 110 * 100:.0f}% coverage)")

    print("\n" + "=" * 60)
    print("=== Counterfactual under each survival model ===")
    print("=" * 60)

    # Full dataset
    all_results = analyze_slice(fills, outcomes, "ALL (full dataset)")

    # Per-prefix slices, in fixed order; only slices that exist in data
    prefixes_present = sorted({f["_prefix"] for f in fills})
    per_prefix_results = {}
    for prefix in prefixes_present:
        slice_fills = [f for f in fills if f["_prefix"] == prefix]
        per_prefix_results[prefix] = analyze_slice(
            slice_fills, outcomes, f"prefix={prefix}"
        )

    # Comparison summary — each cell shows (ratio, P&L $); baseline column
    # shows the hold-to-settle P&L with no penalty applied. The +/- on the
    # penalty columns is vs baseline P&L.
    print("\n" + "=" * 60)
    print("=== Comparison summary: yes:no ratio + total P&L ===")
    print("=" * 60)
    print(f"{'Slice':<6s}  {'Baseline':>22s}  "
          f"{'pessimistic':>22s}  {'base':>22s}  {'optimistic':>22s}")
    for label, results in [("ALL", all_results)] + [
        (p, per_prefix_results[p]) for p in prefixes_present
    ]:
        b = results.get("_baseline")
        if b is None:
            print(f"{label:<6s}  (empty slice)")
            continue
        base_ratio = b["ratio"]
        base_pnl_dollars = b["pnl_c"] / 100
        base_str = f"r={base_ratio:>5.2f} ${base_pnl_dollars:>+7.2f}"
        cells = []
        for m in SURVIVAL_MODELS:
            r = results.get(m)
            if r is None:
                cells.append(f"{'-':>22s}")
                continue
            cells.append(
                f"r={r['ratio']:>5.2f} ${r['pnl_c']/100:>+7.2f} "
                f"Δ${r['delta_vs_baseline']/100:>+6.2f}"
            )
        print(f"{label:<6s}  {base_str:>22s}  " + "  ".join(cells))

    conn.close()


if __name__ == "__main__":
    main()
