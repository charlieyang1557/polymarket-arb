#!/usr/bin/env python3
"""Phase 1.1: Polymarket sports calibration vs realized settlement.

Loads resolved markets from data/historical/price_history.json, filters to sports,
buckets pre-game prices in 30-70c band, and compares implied probability to actual
settlement rate. Bootstrap CIs for honesty about small-sample uncertainty.

NOTE: This is fundamentally different from data/strategy_a/* which compares
Polymarket prices to Pinnacle. Here we compare Polymarket implied prob to
realized settlement outcome (paper's calibration definition).
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_PATH = Path("data/historical/price_history.json")
OUTPUT_DIR = Path("data/research")
DEFAULT_BAND = (0.30, 0.70)
WIDER_BAND = (0.25, 0.75)
DEFAULT_BUCKET = 0.05
MIN_SAMPLE = 500


# ---------- pure parsing helpers ----------

def parse_categories(market: dict) -> list:
    """category_keywords may be a Python list, or a string repr like "['sports']"."""
    raw = market.get("category_keywords")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            value = ast.literal_eval(raw)
            return value if isinstance(value, list) else []
        except (ValueError, SyntaxError):
            return []
    return []


def is_sports_market(market: dict) -> bool:
    return "sports" in parse_categories(market)


def extract_resolved(markets: list, allow_fallback: bool = False) -> list:
    """Pull resolved markets with valid pre-game price.

    Returns dicts with keys: price (float 0-1), settled_yes (bool), volume,
    closed_time, slug, price_source.
    """
    out = []
    for m in markets:
        did_yes = m.get("did_yes_win")
        if did_yes is None:
            continue

        price = m.get("price_24h_before")
        source = "24h_before"
        if price is None:
            if not allow_fallback:
                continue
            price = m.get("price_midlife")
            source = "midlife_fallback"
            if price is None:
                continue

        try:
            price = float(price)
        except (TypeError, ValueError):
            continue

        if price <= 0 or price >= 1:
            continue

        out.append({
            "price": price,
            "settled_yes": bool(did_yes),
            "volume": float(m.get("volume", 0) or 0),
            "closed_time": m.get("closed_time"),
            "slug": m.get("slug", ""),
            "question": (m.get("question") or "")[:120],
            "price_source": source,
            "categories": parse_categories(m),
        })
    return out


# ---------- bucketing ----------

def assign_bucket(price: float, low: float, high: float, size: float):
    """Return integer bucket index, or None if price is outside [low, high)."""
    if price is None:
        return None
    if price < low or price >= high:
        return None
    return int((price - low) / size)


def bucketize(markets: list, low: float, high: float, size: float) -> dict:
    """Group markets by bucket index. Markets outside [low, high) are dropped."""
    buckets = defaultdict(list)
    for m in markets:
        idx = assign_bucket(m["price"], low, high, size)
        if idx is not None:
            buckets[idx].append(m)
    return dict(buckets)


def compute_bucket_stats(markets: list) -> dict:
    n = len(markets)
    if n == 0:
        return {"n": 0, "wins": 0, "settle_yes_rate": 0.0, "mean_price": 0.0}
    wins = sum(1 for m in markets if m["settled_yes"])
    mean_price = sum(m["price"] for m in markets) / n
    return {
        "n": n,
        "wins": wins,
        "settle_yes_rate": wins / n,
        "mean_price": mean_price,
    }


# ---------- bootstrap CI ----------

def bootstrap_settle_rate_ci(markets: list, n_resample: int = 1000,
                              alpha: float = 0.05, seed: int = 42) -> tuple:
    """Percentile-bootstrap CI on the settle-yes rate. Returns (lo, hi)."""
    n = len(markets)
    if n == 0:
        return (0.0, 1.0)
    rng = random.Random(seed)
    outcomes = [1 if m["settled_yes"] else 0 for m in markets]
    rates = []
    for _ in range(n_resample):
        sample = [outcomes[rng.randrange(n)] for _ in range(n)]
        rates.append(sum(sample) / n)
    rates.sort()
    lo_idx = int(math.floor((alpha / 2) * n_resample))
    hi_idx = int(math.ceil((1 - alpha / 2) * n_resample)) - 1
    lo_idx = max(0, min(lo_idx, n_resample - 1))
    hi_idx = max(0, min(hi_idx, n_resample - 1))
    return (rates[lo_idx], rates[hi_idx])


# ---------- precheck ----------

def precheck_sample_size(markets: list,
                          main_band: tuple = DEFAULT_BAND,
                          wider_band: tuple = WIDER_BAND,
                          min_n: int = MIN_SAMPLE) -> dict:
    """Decide whether main or wider band has sufficient sample size."""
    in_main = [m for m in markets if main_band[0] <= m["price"] < main_band[1]]
    in_wider = [m for m in markets if wider_band[0] <= m["price"] < wider_band[1]]
    n_main = len(in_main)
    n_wider = len(in_wider)

    if n_main >= min_n:
        return {"ok": True, "n_main": n_main, "n_wider": n_wider,
                "fallback": None, "band": main_band}
    if n_wider >= min_n:
        return {"ok": True, "n_main": n_main, "n_wider": n_wider,
                "fallback": "widen_band", "band": wider_band}
    return {"ok": False, "n_main": n_main, "n_wider": n_wider,
            "fallback": "aggregate_only", "band": main_band}


# ---------- summary ----------

def summarize_calibration(markets: list, low: float, high: float, size: float,
                           n_resample: int = 1000, alpha: float = 0.05,
                           seed: int = 42) -> dict:
    """Run full calibration: per-bucket stats + bootstrap CIs + max miscalibration."""
    n_total = len(markets)
    if n_total == 0:
        return {"n_total": 0, "buckets": [], "max_miscalibration_pp": 0,
                "aggregate": {"n": 0, "settle_yes_rate": 0.0, "mean_price": 0.0}}

    buckets = bucketize(markets, low, high, size)
    rows = []
    max_gap_pp = 0.0
    for idx in sorted(buckets.keys()):
        ms = buckets[idx]
        stats = compute_bucket_stats(ms)
        ci_lo, ci_hi = bootstrap_settle_rate_ci(ms, n_resample=n_resample,
                                                 alpha=alpha, seed=seed + idx)
        bucket_low = low + idx * size
        bucket_high = bucket_low + size
        bucket_mid = (bucket_low + bucket_high) / 2
        # Implied probability is the mean price (volume-weighted not used here; equal-weighted)
        implied = stats["mean_price"]
        gap_pp = (stats["settle_yes_rate"] - implied) * 100
        if abs(gap_pp) > abs(max_gap_pp):
            max_gap_pp = gap_pp
        # Significance: does CI exclude the implied probability?
        ci_excludes_implied = (implied < ci_lo) or (implied > ci_hi)
        rows.append({
            "bucket_low": round(bucket_low, 2),
            "bucket_high": round(bucket_high, 2),
            "bucket_mid": round(bucket_mid, 2),
            "n": stats["n"],
            "wins": stats["wins"],
            "settle_yes_rate": round(stats["settle_yes_rate"], 4),
            "mean_price": round(implied, 4),
            "ci_lo": round(ci_lo, 4),
            "ci_hi": round(ci_hi, 4),
            "gap_pp": round(gap_pp, 2),
            "ci_excludes_implied": ci_excludes_implied,
        })

    # Aggregate across all in-band markets
    in_band = [m for m in markets if low <= m["price"] < high]
    agg_stats = compute_bucket_stats(in_band)
    agg_ci_lo, agg_ci_hi = bootstrap_settle_rate_ci(
        in_band, n_resample=n_resample, alpha=alpha, seed=seed)

    return {
        "n_total": n_total,
        "n_in_band": len(in_band),
        "band": [low, high],
        "bucket_size": size,
        "buckets": rows,
        "max_miscalibration_pp": round(abs(max_gap_pp), 2),
        "max_miscalibration_signed_pp": round(max_gap_pp, 2),
        "aggregate": {
            "n": agg_stats["n"],
            "settle_yes_rate": round(agg_stats["settle_yes_rate"], 4),
            "mean_price": round(agg_stats["mean_price"], 4),
            "ci_lo": round(agg_ci_lo, 4),
            "ci_hi": round(agg_ci_hi, 4),
            "aggregate_gap_pp": round((agg_stats["settle_yes_rate"]
                                       - agg_stats["mean_price"]) * 100, 2),
        },
    }


# ---------- top-level pipeline ----------

def load_markets(path: Path = DATA_PATH) -> list:
    with open(path) as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        return list(raw.values())
    return list(raw)


def run(data_path: Path = DATA_PATH, out_dir: Path = OUTPUT_DIR,
        main_band: tuple = DEFAULT_BAND, wider_band: tuple = WIDER_BAND,
        bucket_size: float = DEFAULT_BUCKET, min_n: int = MIN_SAMPLE,
        allow_fallback: bool = True, seed: int = 42) -> dict:
    """End-to-end pipeline. Writes outputs to out_dir, returns the summary dict."""
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = load_markets(data_path)
    sports_raw = [m for m in raw if is_sports_market(m)]
    sports = extract_resolved(sports_raw, allow_fallback=allow_fallback)

    pre = precheck_sample_size(sports, main_band, wider_band, min_n)

    band = pre["band"]
    summary = summarize_calibration(sports, low=band[0], high=band[1],
                                     size=bucket_size, n_resample=1000,
                                     alpha=0.05, seed=seed)
    summary["precheck"] = pre
    summary["data_path"] = str(data_path)
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["sports_total_resolved"] = len(sports)
    summary["raw_markets_in_file"] = len(raw)

    # Write JSON flags
    flags_path = out_dir / "miscalibration_flags.json"
    flags = {
        "summary": summary,
        "buckets": summary["buckets"],
        "aggregate": summary["aggregate"],
        "precheck": pre,
        "thresholds": {
            "tight_lt_pp": 3,
            "meaningful_3_to_8_pp": [3, 8],
            "large_gt_pp": 8,
        },
    }
    with open(flags_path, "w") as f:
        json.dump(flags, f, indent=2)

    # Write markdown summary
    md_path = out_dir / "calibration_summary.md"
    with open(md_path, "w") as f:
        f.write(_render_md(summary))

    return summary


def _render_md(summary: dict) -> str:
    lines = []
    lines.append("# Phase 1.1 — Polymarket sports calibration vs realized settlement")
    lines.append("")
    lines.append(f"Generated: {summary.get('generated_at', '?')}")
    lines.append(f"Data: `{summary.get('data_path', '?')}`")
    lines.append("")
    lines.append("## Sample size precheck")
    pre = summary.get("precheck", {})
    lines.append(f"- Total resolved sports markets in file: **{summary.get('sports_total_resolved', 0)}**")
    lines.append(f"- In main band [0.30, 0.70): **{pre.get('n_main', 0)}**")
    lines.append(f"- In wider band [0.25, 0.75): **{pre.get('n_wider', 0)}**")
    lines.append(f"- Precheck ok: **{pre.get('ok', False)}** | Fallback: **{pre.get('fallback')}**")
    lines.append(f"- Band used for analysis: **{pre.get('band')}**")
    lines.append("")
    lines.append("## Aggregate calibration (in-band, equal-weighted)")
    agg = summary.get("aggregate", {})
    lines.append(f"- n = {agg.get('n', 0)}")
    lines.append(f"- Mean implied price: {agg.get('mean_price', 0):.3f}")
    lines.append(f"- Realized YES-settle rate: {agg.get('settle_yes_rate', 0):.3f}")
    lines.append(f"- 95% bootstrap CI on settle rate: [{agg.get('ci_lo', 0):.3f}, {agg.get('ci_hi', 0):.3f}]")
    lines.append(f"- Aggregate gap (realized − implied): **{agg.get('aggregate_gap_pp', 0):+.2f}pp**")
    lines.append("")
    lines.append("## Per-bucket (size = {}c)".format(int(summary.get("bucket_size", 0.05) * 100)))
    lines.append("")
    lines.append("| Bucket | n | Wins | Settle YES rate | Mean price | 95% CI | Gap (pp) | CI excludes implied? |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in summary.get("buckets", []):
        lo = r["bucket_low"]
        hi = r["bucket_high"]
        ci = f"[{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]"
        excl = "✓" if r["ci_excludes_implied"] else ""
        lines.append(f"| {lo:.2f}-{hi:.2f} | {r['n']} | {r['wins']} | {r['settle_yes_rate']:.3f} | {r['mean_price']:.3f} | {ci} | {r['gap_pp']:+.2f} | {excl} |")
    lines.append("")
    lines.append("## Decision gate (per plan, derived from break-even economics ~2-3c)")
    lines.append(f"- Max miscalibration in band: **{summary.get('max_miscalibration_pp', 0):.2f}pp** (signed: {summary.get('max_miscalibration_signed_pp', 0):+.2f}pp)")
    max_pp = summary.get("max_miscalibration_pp", 0)
    if pre.get("ok") is False:
        lines.append("- **VERDICT: insufficient data — aggregate-only result, treat as preliminary**")
    elif max_pp < 3:
        lines.append("- **VERDICT: tight (<3pp) — behavioral edge insufficient on this evidence**")
    elif max_pp <= 8:
        lines.append("- **VERDICT: meaningful (3-8pp) — offensive hybrid viable**")
    else:
        lines.append("- **VERDICT: large (>8pp) — offensive hybrid strongly justified**")
    lines.append("")
    lines.append("## Caveats")
    lines.append("- Sports is NOT 'single-name' per paper Appendix B — expected miscalibration is smaller than paper's headline 25pp.")
    lines.append("- Equal-weighted bucket statistics; volume weighting may differ.")
    lines.append("- Pre-game price defined as `price_24h_before`; midlife fallback used when null.")
    lines.append("- This measures Polymarket-vs-realized-settlement, NOT Polymarket-vs-Pinnacle (different question from strategy_a).")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--bucket-size", type=float, default=DEFAULT_BUCKET)
    parser.add_argument("--min-n", type=int, default=MIN_SAMPLE)
    parser.add_argument("--no-fallback", action="store_true",
                        help="Disable midlife price fallback")
    args = parser.parse_args()

    summary = run(data_path=args.data, out_dir=args.out,
                  bucket_size=args.bucket_size, min_n=args.min_n,
                  allow_fallback=not args.no_fallback)

    print("=" * 70)
    print("PHASE 1.1 — CALIBRATION SUMMARY")
    print("=" * 70)
    print(f"Total resolved sports markets: {summary['sports_total_resolved']}")
    print(f"In band {summary['precheck']['band']}: {summary['precheck']['n_main']}")
    print(f"Precheck ok: {summary['precheck']['ok']} | fallback: {summary['precheck']['fallback']}")
    print(f"Aggregate gap: {summary['aggregate']['aggregate_gap_pp']:+.2f}pp")
    print(f"Max bucket miscalibration: {summary['max_miscalibration_pp']:.2f}pp")
    print(f"Output: {args.out / 'calibration_summary.md'}")
    print(f"Output: {args.out / 'miscalibration_flags.json'}")


if __name__ == "__main__":
    main()
