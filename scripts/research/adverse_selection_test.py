#!/usr/bin/env python3
"""Phase 1.3: Round-trip viability test.

Applies paper Section 4.2.3 to our actual fills: is the half-spread we capture
sufficient to absorb the informed-taker share we observe?

Multi-window informed proxy:
  - Short:  30s post-fill adverse mid move ≥ 1c
  - Medium: 2min post-fill adverse mid move ≥ 2c
  - Long:   5min post-fill adverse mid move ≥ 3c
  - Union as informed proxy

Decision:
  α_tolerance = avg_half_spread / avg_loss_distance
  α_observed  = fraction of fills meeting any informed criterion
  α_tolerance > α_observed × 1.5 → viable
  α_tolerance ≈ α_observed       → marginal
  α_tolerance < α_observed       → structurally negative
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("/Users/openclaw/polymarket-arb/data/poly_mm_live.db")
DEFAULT_OUT = Path("data/research")

# Multi-window thresholds
THRESH_30S = 1.0   # cents
THRESH_2MIN = 2.0
THRESH_5MIN = 3.0


# ---------- pure helpers ----------

def half_spread_captured(side: str, price_cents: int, midpoint: float) -> float:
    """Captured half-spread in cents.

    For yes_bid: midpoint − our_bid (positive = bought below mid)
    For no_bid:  the implied no_price is (100 − midpoint); half_spread = no_price − our_bid
    """
    if midpoint is None:
        return 0.0
    if side == "yes_bid":
        return float(midpoint - price_cents)
    elif side == "no_bid":
        return float((100 - midpoint) - price_cents)
    else:
        return 0.0


def adverse_move(side: str, mid_at_fill, mid_later) -> float:
    """Adverse mid move in cents (positive number = adverse).

    For yes_bid (long YES): adverse = mid_at_fill − mid_later (mid dropped after we bought)
    For no_bid  (long NO):  adverse = mid_later − mid_at_fill (mid rose, NO price dropped)
    """
    if mid_at_fill is None or mid_later is None:
        return 0.0
    if side == "yes_bid":
        return max(0.0, float(mid_at_fill) - float(mid_later))
    elif side == "no_bid":
        return max(0.0, float(mid_later) - float(mid_at_fill))
    return 0.0


def classify_informed(adv_30s: float, adv_2min: float, adv_5min: float) -> bool:
    """Union of three windows: any meets-or-exceeds threshold → informed."""
    return (adv_30s >= THRESH_30S or
            adv_2min >= THRESH_2MIN or
            adv_5min >= THRESH_5MIN)


def parse_iso_to_ts(iso: str) -> float:
    """Parse ISO 8601 string (with offset) to POSIX seconds."""
    if iso is None:
        return 0.0
    # Strip subsecond microseconds beyond what fromisoformat handles
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def nearest_snapshot(snapshots: list, target_ts: float, max_age_s: float = 30) -> dict | None:
    """Find the snapshot closest in time to target_ts within max_age_s."""
    if not snapshots:
        return None
    times = [s["ts"] for s in snapshots]
    idx = bisect.bisect_right(times, target_ts)
    candidates = []
    if idx > 0:
        candidates.append(snapshots[idx - 1])
    if idx < len(snapshots):
        candidates.append(snapshots[idx])
    best = None
    best_age = float("inf")
    for c in candidates:
        age = abs(c["ts"] - target_ts)
        if age <= max_age_s and age < best_age:
            best = c
            best_age = age
    return best


def compute_alpha_tolerance(avg_half_spread: float, avg_loss_distance: float) -> float:
    if avg_half_spread <= 0:
        return 0.0
    if avg_loss_distance <= 0:
        return 1.0
    return float(avg_half_spread / avg_loss_distance)


def compute_alpha_observed(fills: list) -> float:
    if not fills:
        return 0.0
    informed = sum(1 for f in fills if f.get("is_informed"))
    return informed / len(fills)


def summarize_viability(fills: list, margin: float = 1.5) -> dict:
    if not fills:
        return {"n_fills": 0, "verdict": "no_data",
                "alpha_tolerance": 0.0, "alpha_observed": 0.0,
                "mean_half_spread_cents": 0.0,
                "mean_loss_distance_cents": 0.0}

    half_spreads = [f["half_spread_cents"] for f in fills]
    loss_distances = [f.get("loss_distance_cents", 0) for f in fills]

    avg_hs = sum(half_spreads) / len(half_spreads)
    # Use loss_distance only when fill was informed (the "wrong-way" cost when adverse selection bites)
    informed_distances = [f.get("loss_distance_cents", 0) for f in fills if f.get("is_informed")]
    if informed_distances:
        avg_loss_distance = sum(informed_distances) / len(informed_distances)
    else:
        # Fallback: average distance to nearest 0/100 boundary (paper's 100-P proxy)
        avg_loss_distance = sum(min(f["price_cents"], 100 - f["price_cents"]) for f in fills) / len(fills)

    alpha_tol = compute_alpha_tolerance(avg_hs, avg_loss_distance)
    alpha_obs = compute_alpha_observed(fills)

    if alpha_tol >= alpha_obs * margin and alpha_tol > 0:
        verdict = "viable"
    elif alpha_tol >= alpha_obs * 0.9:
        verdict = "marginal"
    else:
        verdict = "negative"

    return {
        "n_fills": len(fills),
        "mean_half_spread_cents": avg_hs,
        "mean_loss_distance_cents": avg_loss_distance,
        "alpha_tolerance": alpha_tol,
        "alpha_observed": alpha_obs,
        "alpha_observed_x_margin": alpha_obs * margin,
        "verdict": verdict,
    }


# ---------- DB / pipeline ----------

def load_fills(db_path: Path) -> list:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ticker, side, price, size, fee, filled_at "
        "FROM mm_fills WHERE side != 'settlement' ORDER BY filled_at"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "ticker": r["ticker"],
            "side": r["side"],
            "price_cents": int(r["price"]),
            "size": int(r["size"]),
            "fee": float(r["fee"]),
            "filled_at": r["filled_at"],
            "filled_ts": parse_iso_to_ts(r["filled_at"]),
        })
    conn.close()
    return out


def load_snapshots_per_ticker(db_path: Path, tickers: set) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT ticker, ts, midpoint FROM mm_snapshots "
        f"WHERE ticker IN ({placeholders}) AND midpoint IS NOT NULL "
        f"ORDER BY ticker, ts",
        list(tickers)
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        out[r["ticker"]].append({
            "ts": parse_iso_to_ts(r["ts"]),
            "midpoint": float(r["midpoint"]),
        })
    conn.close()
    return dict(out)


def annotate_fills_with_drift(fills: list, snapshots_by_ticker: dict) -> list:
    out = []
    for f in fills:
        snaps = snapshots_by_ticker.get(f["ticker"], [])

        snap_at_fill = nearest_snapshot(snaps, f["filled_ts"], max_age_s=15)
        snap_30s = nearest_snapshot(snaps, f["filled_ts"] + 30, max_age_s=20)
        snap_2min = nearest_snapshot(snaps, f["filled_ts"] + 120, max_age_s=30)
        snap_5min = nearest_snapshot(snaps, f["filled_ts"] + 300, max_age_s=60)

        mid_at_fill = snap_at_fill["midpoint"] if snap_at_fill else None
        mid_30s = snap_30s["midpoint"] if snap_30s else None
        mid_2min = snap_2min["midpoint"] if snap_2min else None
        mid_5min = snap_5min["midpoint"] if snap_5min else None

        hs = half_spread_captured(f["side"], f["price_cents"], mid_at_fill) if mid_at_fill is not None else 0.0
        adv_30s = adverse_move(f["side"], mid_at_fill, mid_30s)
        adv_2min = adverse_move(f["side"], mid_at_fill, mid_2min)
        adv_5min = adverse_move(f["side"], mid_at_fill, mid_5min)
        is_informed = classify_informed(adv_30s, adv_2min, adv_5min)
        # Loss distance: largest of the adverse moves observed (proxy for downside)
        loss_distance = max(adv_30s, adv_2min, adv_5min)

        out.append({
            **f,
            "mid_at_fill": mid_at_fill,
            "mid_30s": mid_30s,
            "mid_2min": mid_2min,
            "mid_5min": mid_5min,
            "half_spread_cents": hs,
            "adv_30s": adv_30s,
            "adv_2min": adv_2min,
            "adv_5min": adv_5min,
            "is_informed": is_informed,
            "loss_distance_cents": loss_distance,
            "snapshot_coverage": all([mid_at_fill, mid_30s, mid_2min, mid_5min]),
        })
    return out


def stratify(fills: list, key_fn) -> dict:
    out = defaultdict(list)
    for f in fills:
        k = key_fn(f)
        out[k].append(f)
    return dict(out)


def run(db_path: Path = DEFAULT_DB, out_dir: Path = DEFAULT_OUT) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    fills = load_fills(db_path)
    print(f"Loaded {len(fills)} fills")
    tickers = {f["ticker"] for f in fills}
    snapshots = load_snapshots_per_ticker(db_path, tickers)
    print(f"Loaded snapshots for {len(snapshots)} tickers")

    annotated = annotate_fills_with_drift(fills, snapshots)
    n_with_full_coverage = sum(1 for f in annotated if f["snapshot_coverage"])
    print(f"Fills with full snapshot coverage (mid + 30s + 2m + 5m): {n_with_full_coverage}/{len(annotated)}")

    overall = summarize_viability(annotated)

    # By midpoint band (35-45, 45-55, 55-65)
    by_band = {}
    for label, lo, hi in [("35-45", 35, 45), ("45-55", 45, 55), ("55-65", 55, 65)]:
        sub = [f for f in annotated if f["mid_at_fill"] is not None
               and lo <= f["mid_at_fill"] < hi]
        by_band[label] = summarize_viability(sub)

    # By side
    by_side = {}
    for s in ("yes_bid", "no_bid"):
        sub = [f for f in annotated if f["side"] == s]
        by_side[s] = summarize_viability(sub)

    # By informed classification breakdown for diagnostics
    informed_count = sum(1 for f in annotated if f["is_informed"])
    only_30s = sum(1 for f in annotated if f["adv_30s"] >= THRESH_30S
                   and f["adv_2min"] < THRESH_2MIN and f["adv_5min"] < THRESH_5MIN)
    only_2min = sum(1 for f in annotated if f["adv_2min"] >= THRESH_2MIN
                    and f["adv_30s"] < THRESH_30S and f["adv_5min"] < THRESH_5MIN)
    only_5min = sum(1 for f in annotated if f["adv_5min"] >= THRESH_5MIN
                    and f["adv_30s"] < THRESH_30S and f["adv_2min"] < THRESH_2MIN)
    multi_window = sum(1 for f in annotated if (f["adv_30s"] >= THRESH_30S)
                       + (f["adv_2min"] >= THRESH_2MIN)
                       + (f["adv_5min"] >= THRESH_5MIN) >= 2)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "n_fills": len(annotated),
        "n_with_full_snapshot_coverage": n_with_full_coverage,
        "thresholds": {"30s": THRESH_30S, "2min": THRESH_2MIN, "5min": THRESH_5MIN},
        "overall": overall,
        "by_midpoint_band": by_band,
        "by_side": by_side,
        "informed_breakdown": {
            "any_window": informed_count,
            "only_30s": only_30s,
            "only_2min": only_2min,
            "only_5min": only_5min,
            "multi_window": multi_window,
        },
    }

    with open(out_dir / "round_trip_viability.md", "w") as f:
        f.write(_render_md(summary))

    with open(out_dir / "round_trip_viability.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    return summary


def _render_md(s: dict) -> str:
    lines = []
    lines.append("# Phase 1.3 — Round-trip viability test")
    lines.append("")
    lines.append(f"Generated: {s['generated_at']}")
    lines.append(f"Source DB: `{s['db_path']}`")
    lines.append("")
    lines.append("## Sample")
    lines.append(f"- Fills: {s['n_fills']}")
    lines.append(f"- Full snapshot coverage (mid+30s+2min+5min): {s['n_with_full_snapshot_coverage']}/{s['n_fills']}")
    t = s["thresholds"]
    lines.append(f"- Adverse-move thresholds: 30s≥{t['30s']}c, 2min≥{t['2min']}c, 5min≥{t['5min']}c")
    lines.append("")
    lines.append("## Overall")
    o = s["overall"]
    lines.append(f"- Mean half-spread captured: {o['mean_half_spread_cents']:+.2f}c")
    lines.append(f"- Mean loss distance (informed-fill subset): {o['mean_loss_distance_cents']:+.2f}c")
    lines.append(f"- α_tolerance = {o['alpha_tolerance']:.4f}")
    lines.append(f"- α_observed (informed share) = {o['alpha_observed']:.4f}")
    lines.append(f"- α_observed × 1.5 margin = {o['alpha_observed_x_margin']:.4f}")
    lines.append(f"- **VERDICT: {o['verdict']}**")
    lines.append("")
    lines.append("## By midpoint band")
    for label, sub in s["by_midpoint_band"].items():
        lines.append(f"- {label}: n={sub['n_fills']}, half_spread={sub['mean_half_spread_cents']:+.2f}c, "
                     f"loss_dist={sub['mean_loss_distance_cents']:.2f}c, "
                     f"α_tol={sub['alpha_tolerance']:.3f}, α_obs={sub['alpha_observed']:.3f}, verdict={sub['verdict']}")
    lines.append("")
    lines.append("## By side")
    for k, sub in s["by_side"].items():
        lines.append(f"- {k}: n={sub['n_fills']}, half_spread={sub['mean_half_spread_cents']:+.2f}c, "
                     f"α_tol={sub['alpha_tolerance']:.3f}, α_obs={sub['alpha_observed']:.3f}, verdict={sub['verdict']}")
    lines.append("")
    ib = s["informed_breakdown"]
    lines.append("## Informed classification breakdown")
    lines.append(f"- Total informed (any window): {ib['any_window']} ({ib['any_window']/s['n_fills']*100:.1f}%)")
    lines.append(f"- Only 30s: {ib['only_30s']} | Only 2min: {ib['only_2min']} | Only 5min: {ib['only_5min']}")
    lines.append(f"- Multi-window (≥2 windows triggered): {ib['multi_window']}")
    lines.append("")
    lines.append("## Decision interpretation")
    if o["verdict"] == "viable":
        lines.append("Round-trip strategy is structurally viable on its own. Hold-to-settle is additive, not necessary.")
    elif o["verdict"] == "marginal":
        lines.append("Round-trip is marginal. Consider hold-to-settle as additive edge.")
    elif o["verdict"] == "negative":
        lines.append("Round-trip is structurally NEGATIVE: spread captured cannot absorb observed informed flow. ")
        lines.append("Either pivot strategy or accept that current bot is not edge-positive on this dimension.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    summary = run(args.db, args.out)
    o = summary["overall"]
    print()
    print("=" * 70)
    print("PHASE 1.3 — ROUND-TRIP VIABILITY")
    print("=" * 70)
    print(f"Fills: {summary['n_fills']} (full coverage: {summary['n_with_full_snapshot_coverage']})")
    print(f"Mean half-spread: {o['mean_half_spread_cents']:+.2f}c")
    print(f"α_tolerance: {o['alpha_tolerance']:.4f} | α_observed: {o['alpha_observed']:.4f}")
    print(f"VERDICT: {o['verdict']}")


if __name__ == "__main__":
    main()
