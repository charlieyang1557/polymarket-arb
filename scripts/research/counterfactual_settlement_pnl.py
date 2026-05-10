#!/usr/bin/env python3
"""Phase 1.2: Counterfactual settlement P&L for bot fills.

Reads mm_fills from poly_mm_live.db, looks up each unique slug's resolution
via the Polymarket SDK (cached to JSON), and computes hypothetical settlement
P&L as if every fill had been held to settlement instead of force-exited.

CRITICAL CAVEAT: Counterfactual is an UPPER BOUND.
A real offensive strategy will accumulate at less-favorable prices, on
different markets (selected for miscalibration not round-trip favorability),
and not all flagged markets will fill. We apply a 50% capture-rate haircut
to produce a more honest decision number, decomposed as:
  selection (0.7) × slippage (0.9) × hit_rate (0.8) ≈ 0.50

Decision gate is the post-haircut number.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("/Users/openclaw/polymarket-arb/data/poly_mm_live.db")
DEFAULT_OUT = Path("data/research")
DEFAULT_CACHE = DEFAULT_OUT / "resolved_markets_cache.json"
CAPTURE_HAIRCUT = 0.50  # 0.7 selection × 0.9 slippage × 0.8 hit-rate


# ---------- pure functions ----------

def parse_resolution(market: dict) -> str | None:
    """Return 'yes', 'no', or None depending on long-side settlement.

    Uses marketSides[long=true].price first; falls back to outcomePrices[0]
    when marketSides is empty/missing. Polymarket convention: outcomes[0]
    corresponds to the long/YES side.
    """
    if not market or not isinstance(market, dict):
        return None
    sides = market.get("marketSides") or []
    if isinstance(sides, list) and sides:
        for s in sides:
            if not isinstance(s, dict):
                continue
            if s.get("long") is True:
                price = str(s.get("price", "")).strip()
                if price == "1":
                    return "yes"
                if price == "0":
                    return "no"
                return None  # unsettled or ambiguous

    outcome_prices = market.get("outcomePrices")
    if isinstance(outcome_prices, list) and len(outcome_prices) >= 1:
        first = str(outcome_prices[0]).strip()
        if first == "1":
            return "yes"
        if first == "0":
            return "no"
    return None


def fill_settlement_pnl(side: str, price_cents: int, size: int,
                         settled_yes: bool) -> int:
    """Counterfactual P&L in cents for a single fill held to settlement.

    side ∈ {yes_bid, no_bid}: which side the bot bought via maker bid.
    price_cents: per-contract entry price.
    size: number of contracts.
    settled_yes: True if the long/YES outcome won.
    """
    if size == 0:
        return 0
    if side == "yes_bid":
        if settled_yes:
            return (100 - price_cents) * size
        else:
            return -price_cents * size
    elif side == "no_bid":
        if settled_yes:
            return -price_cents * size
        else:
            return (100 - price_cents) * size
    else:
        raise ValueError(f"Unknown side: {side}")


def classify_aligned(side: str, settled_yes: bool) -> bool:
    """True if the bought side aligns with the winner."""
    if side == "yes_bid":
        return settled_yes
    elif side == "no_bid":
        return not settled_yes
    raise ValueError(f"Unknown side: {side}")


def decompose_frequency_magnitude(fills_with_pnl: list) -> dict:
    """Paper Eq 7: frequency edge + magnitude edge per contract.

    Each fill dict needs `pnl_cents` (total for the fill, signed) and `size`.
    Win/loss is determined per contract (sign of pnl/contract).
    """
    n_contracts = sum(f["size"] for f in fills_with_pnl)
    if n_contracts == 0:
        return {"n_contracts": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "avg_win_cents": 0.0, "avg_loss_cents": 0.0,
                "frequency_edge_cents": 0.0, "magnitude_edge_cents": 0.0,
                "mean_pnl_cents": 0.0, "total_pnl_cents": 0.0}

    wins = 0
    losses = 0
    total_win_pnl = 0
    total_loss_magnitude = 0
    total_pnl = 0
    for f in fills_with_pnl:
        pnl = f["pnl_cents"]
        size = f["size"]
        per_contract = pnl / size if size > 0 else 0
        total_pnl += pnl
        if per_contract > 0:
            wins += size
            total_win_pnl += pnl
        elif per_contract < 0:
            losses += size
            total_loss_magnitude += -pnl
        else:
            # tie at zero per contract — half wins, half losses for paper symmetry
            wins += size // 2
            losses += size - (size // 2)

    win_rate = wins / n_contracts
    avg_win = (total_win_pnl / wins) if wins > 0 else 0.0
    avg_loss = (total_loss_magnitude / losses) if losses > 0 else 0.0
    # Paper Eq 7: E[π] = (WR - 0.5)(W+L) + 0.5(W-L)
    frequency_edge = (win_rate - 0.5) * (avg_win + avg_loss)
    magnitude_edge = 0.5 * (avg_win - avg_loss)

    return {
        "n_contracts": n_contracts,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_win_cents": avg_win,
        "avg_loss_cents": avg_loss,
        "frequency_edge_cents": frequency_edge,
        "magnitude_edge_cents": magnitude_edge,
        "mean_pnl_cents": total_pnl / n_contracts,
        "total_pnl_cents": total_pnl,
    }


def apply_capture_haircut(pnl_cents: float, multiplier: float = CAPTURE_HAIRCUT) -> float:
    """Apply selection-bias haircut.

    Conservative interpretation: gains shrink by haircut, losses preserved fully.
    A real offensive strategy still pays full losses but may not capture full upside.
    """
    if pnl_cents >= 0:
        return pnl_cents * multiplier
    return pnl_cents


def derive_unhedged_subset(fills: list) -> list:
    """Per-market unhedged inventory at end of trading window.

    Returns a list of dicts: {ticker, net_yes_inventory, net_side}.
    Markets where yes_bid total == no_bid total (net 0) are excluded.
    """
    by_ticker = defaultdict(lambda: {"yes": 0, "no": 0, "fills": []})
    for f in fills:
        by_ticker[f["ticker"]]["fills"].append(f)
        if f["side"] == "yes_bid":
            by_ticker[f["ticker"]]["yes"] += f["size"]
        elif f["side"] == "no_bid":
            by_ticker[f["ticker"]]["no"] += f["size"]
    out = []
    for ticker, agg in by_ticker.items():
        net = agg["yes"] - agg["no"]
        if net == 0:
            continue
        out.append({
            "ticker": ticker,
            "net_yes_inventory": net,
            "net_side": "yes" if net > 0 else "no",
            "n_yes_bids": agg["yes"],
            "n_no_bids": agg["no"],
        })
    return out


def aggregate_by_key(fills_with_pnl: list, key: str) -> dict:
    """Aggregate total P&L, contracts, and win rate per group."""
    by = defaultdict(lambda: {"total_pnl_cents": 0.0, "n_contracts": 0,
                               "wins": 0, "losses": 0, "n_fills": 0})
    for f in fills_with_pnl:
        k = f.get(key, "unknown")
        by[k]["total_pnl_cents"] += f["pnl_cents"]
        by[k]["n_contracts"] += f["size"]
        by[k]["n_fills"] += 1
        per = f["pnl_cents"] / f["size"] if f["size"] > 0 else 0
        if per > 0:
            by[k]["wins"] += f["size"]
        elif per < 0:
            by[k]["losses"] += f["size"]
    for k in by:
        n = by[k]["n_contracts"]
        by[k]["mean_pnl_cents"] = by[k]["total_pnl_cents"] / n if n else 0.0
        by[k]["win_rate"] = by[k]["wins"] / n if n else 0.0
    return dict(by)


# ---------- DB / SDK glue ----------

def load_fills_from_db(db_path: Path, exclude_settlement: bool = True) -> list:
    """Read non-settlement fills from mm_fills."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    where = "WHERE side != 'settlement'" if exclude_settlement else ""
    rows = conn.execute(
        f"SELECT id, session_id, ticker, side, price, size, fee, is_taker, "
        f"inventory_after, pair_id, pair_pnl, filled_at "
        f"FROM mm_fills {where} ORDER BY filled_at"
    ).fetchall()
    fills = []
    for r in rows:
        fills.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "ticker": r["ticker"],
            "side": r["side"],
            "price_cents": int(r["price"]),
            "size": int(r["size"]),
            "fee": float(r["fee"]),  # NB: stored Kalshi-style positive cost
            "is_taker": int(r["is_taker"]),
            "inventory_after": r["inventory_after"],
            "pair_id": r["pair_id"],
            "pair_pnl": r["pair_pnl"],
            "filled_at": r["filled_at"],
        })
    conn.close()
    return fills


def lookup_resolution(slug: str, sdk_client, sleep_s: float = 0.05) -> dict:
    """Query SDK for one slug's resolution status. Returns the parsed data."""
    raw = sdk_client.markets.retrieve_by_slug(slug)
    market = raw.get("market") if isinstance(raw, dict) else None
    if market is None:
        return {"slug": slug, "settled": False, "result": None,
                "closed": False, "category": None, "endDate": None}
    res = parse_resolution(market)
    time.sleep(sleep_s)
    return {
        "slug": slug,
        "settled": res is not None,
        "result": res,
        "closed": bool(market.get("closed")),
        "category": market.get("category"),
        "endDate": market.get("endDate"),
        "ep3Status": market.get("ep3Status"),
        "marketType": market.get("marketType"),
        "looked_up_at": datetime.now(timezone.utc).isoformat(),
    }


def lookup_all_resolutions(slugs: list, cache_path: Path,
                            force_refresh: bool = False) -> dict:
    """Look up resolution per slug, cached to JSON."""
    cache = {}
    if cache_path.exists() and not force_refresh:
        with open(cache_path) as f:
            cache = json.load(f)

    missing = [s for s in slugs if s not in cache]
    if missing:
        sys.path.insert(0, ".")
        from src.poly_client import PolyClient
        client = PolyClient()
        sdk = client.client  # underlying PolymarketUS instance
        for i, slug in enumerate(missing, 1):
            try:
                cache[slug] = lookup_resolution(slug, sdk)
            except Exception as e:
                cache[slug] = {"slug": slug, "settled": False,
                               "result": None, "error": str(e),
                               "looked_up_at": datetime.now(timezone.utc).isoformat()}
            if i % 20 == 0:
                print(f"  Resolved {i}/{len(missing)}: {slug}")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(cache, f, indent=2)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)
    return cache


# ---------- pipeline ----------

def annotate_fills(fills: list, resolutions: dict) -> list:
    """Attach settle_yes per fill; drop fills for unresolved markets."""
    out = []
    skipped = 0
    for f in fills:
        res = resolutions.get(f["ticker"])
        if not res or not res.get("settled"):
            skipped += 1
            continue
        settled_yes = res["result"] == "yes"
        f2 = dict(f)
        f2["settled_yes"] = settled_yes
        f2["pnl_cents"] = fill_settlement_pnl(
            f["side"], f["price_cents"], f["size"], settled_yes)
        f2["aligned"] = classify_aligned(f["side"], settled_yes)
        f2["category"] = res.get("category")
        f2["marketType"] = res.get("marketType")
        out.append(f2)
    return out, skipped


def run(db_path: Path = DEFAULT_DB, out_dir: Path = DEFAULT_OUT,
        cache_path: Path = DEFAULT_CACHE,
        force_refresh: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    fills = load_fills_from_db(db_path)
    print(f"Loaded {len(fills)} fills from {db_path}")
    slugs = sorted({f["ticker"] for f in fills})
    print(f"Distinct slugs: {len(slugs)}")

    resolutions = lookup_all_resolutions(slugs, cache_path,
                                          force_refresh=force_refresh)
    n_settled = sum(1 for r in resolutions.values() if r.get("settled"))
    print(f"Settled markets: {n_settled}/{len(slugs)}")

    annotated, skipped = annotate_fills(fills, resolutions)
    print(f"Annotated fills (resolved markets only): {len(annotated)} (skipped {skipped})")

    # All-fills counterfactual
    all_decomp = decompose_frequency_magnitude(annotated)

    # Defensive subset: per-market unhedged inventory at game-start
    unhedged = derive_unhedged_subset(annotated)
    # For each unhedged market, compute settlement P&L on the net inventory only
    defensive_fills = []
    for u in unhedged:
        res = resolutions.get(u["ticker"])
        if not res or not res.get("settled"):
            continue
        settled_yes = res["result"] == "yes"
        side = "yes_bid" if u["net_side"] == "yes" else "no_bid"
        # Need a representative entry price — use volume-weighted mean of fills on the net side
        net_side_fills = [f for f in annotated
                          if f["ticker"] == u["ticker"] and f["side"] == side]
        if not net_side_fills:
            continue
        total_size = sum(f["size"] for f in net_side_fills)
        if total_size == 0:
            continue
        vw_price = sum(f["price_cents"] * f["size"] for f in net_side_fills) / total_size
        net_size = abs(u["net_yes_inventory"])
        pnl = fill_settlement_pnl(side, int(round(vw_price)), net_size, settled_yes)
        defensive_fills.append({
            "ticker": u["ticker"],
            "side": side,
            "size": net_size,
            "price_cents": int(round(vw_price)),
            "pnl_cents": pnl,
            "aligned": classify_aligned(side, settled_yes),
            "settled_yes": settled_yes,
            "category": res.get("category"),
        })
    defensive_decomp = decompose_frequency_magnitude(defensive_fills)

    # Aggregations
    by_ticker = aggregate_by_key(annotated, "ticker")
    by_side = aggregate_by_key(annotated, "side")
    by_category = aggregate_by_key(annotated, "category")

    # Compare with realized (from pair_pnl)
    realized_pair_pnl = sum((f.get("pair_pnl") or 0) for f in fills)
    fees_paid = sum(f["fee"] for f in fills)

    # Apply haircut on the all-fills mean
    mean_post_haircut = apply_capture_haircut(all_decomp["mean_pnl_cents"])

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "n_fills_total": len(fills),
        "n_distinct_slugs": len(slugs),
        "n_resolved_slugs": n_settled,
        "n_fills_in_resolved": len(annotated),
        "all_fills_counterfactual": all_decomp,
        "all_fills_post_haircut_mean_per_contract_cents": mean_post_haircut,
        "defensive_subset": {
            "n_unhedged_markets": len(unhedged),
            "n_unhedged_resolved": len(defensive_fills),
            "decomposition": defensive_decomp,
        },
        "by_ticker": by_ticker,
        "by_side": by_side,
        "by_category": by_category,
        "realized_pair_pnl_cents": realized_pair_pnl,
        "stored_fees_total_cents": fees_paid,
        "haircut_multiplier": CAPTURE_HAIRCUT,
        "haircut_decomposition": {
            "selection_bias": 0.7,
            "slippage": 0.9,
            "hit_rate": 0.8,
            "combined": CAPTURE_HAIRCUT,
        },
        "fee_convention_note": (
            "DB stores Kalshi-style positive maker fees (formula 0.0175*P*(1-P)*100). "
            "Polymarket actual rebate is NEGATIVE (formula 0.02*0.25*P*(1-P)*100 = "
            "0.005*P*(1-P)*100). Counterfactual P&L computed without fees because: "
            "(a) entry fees are sunk, identical between strategies; "
            "(b) hold-to-settle has no exit fee. Cross-check vs realized requires same fees."
        ),
    }

    with open(out_dir / "settlement_pnl_report.md", "w") as f:
        f.write(_render_md(summary))

    with open(out_dir / "settlement_pnl_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    return summary


def _render_md(s: dict) -> str:
    lines = []
    lines.append("# Phase 1.2 — Counterfactual settlement P&L on bot fills")
    lines.append("")
    lines.append(f"Generated: {s['generated_at']}")
    lines.append(f"Source DB: `{s['db_path']}`")
    lines.append("")
    lines.append("## Coverage")
    lines.append(f"- Total fills: {s['n_fills_total']}")
    lines.append(f"- Distinct markets: {s['n_distinct_slugs']}")
    lines.append(f"- Resolved markets: {s['n_resolved_slugs']}")
    lines.append(f"- Fills in resolved markets: {s['n_fills_in_resolved']}")
    lines.append("")
    lines.append("## All-fills counterfactual (upper bound)")
    a = s["all_fills_counterfactual"]
    lines.append(f"- Contracts: {a['n_contracts']}")
    lines.append(f"- Wins: {a['wins']} | Losses: {a['losses']}")
    lines.append(f"- Win rate: {a['win_rate']*100:.1f}%")
    lines.append(f"- Avg win: {a['avg_win_cents']:.2f}c | Avg loss: {a['avg_loss_cents']:.2f}c")
    lines.append(f"- Frequency edge: {a['frequency_edge_cents']:+.2f}c")
    lines.append(f"- Magnitude edge: {a['magnitude_edge_cents']:+.2f}c")
    lines.append(f"- Mean P&L per contract: **{a['mean_pnl_cents']:+.2f}c**")
    lines.append(f"- Total counterfactual: ${a['total_pnl_cents']/100:+.2f}")
    lines.append("")
    lines.append("## Post-haircut mean (decision number)")
    lines.append(f"- Haircut: 0.7 selection × 0.9 slippage × 0.8 hit-rate = {s['haircut_multiplier']}")
    lines.append(f"- Mean P&L per contract post-haircut: **{s['all_fills_post_haircut_mean_per_contract_cents']:+.2f}c**")
    lines.append("")
    lines.append("## Defensive-only subset (game-start unhedged inventory)")
    d = s["defensive_subset"]
    dd = d["decomposition"]
    lines.append(f"- Unhedged markets: {d['n_unhedged_markets']}")
    lines.append(f"- Resolved unhedged: {d['n_unhedged_resolved']}")
    lines.append(f"- Contracts: {dd['n_contracts']}")
    if dd["n_contracts"] > 0:
        lines.append(f"- Win rate: {dd['win_rate']*100:.1f}%")
        lines.append(f"- Mean P&L per contract: {dd['mean_pnl_cents']:+.2f}c")
        lines.append(f"- Total: ${dd['total_pnl_cents']/100:+.2f}")
    lines.append("")
    lines.append("## Aggregation: by side")
    for k, v in s["by_side"].items():
        lines.append(f"- {k}: n_contracts={v['n_contracts']}, total=${v['total_pnl_cents']/100:+.2f}, "
                     f"mean={v['mean_pnl_cents']:+.2f}c, win_rate={v['win_rate']*100:.1f}%")
    lines.append("")
    lines.append("## Aggregation: by category")
    for k, v in s["by_category"].items():
        lines.append(f"- {k}: n_contracts={v['n_contracts']}, total=${v['total_pnl_cents']/100:+.2f}, "
                     f"mean={v['mean_pnl_cents']:+.2f}c, win_rate={v['win_rate']*100:.1f}%")
    lines.append("")
    lines.append("## Comparison vs realized")
    lines.append(f"- Stored realized pair_pnl total: {s['realized_pair_pnl_cents']:+.2f}c (${s['realized_pair_pnl_cents']/100:+.2f})")
    lines.append(f"- Stored fees total: {s['stored_fees_total_cents']:+.2f}c")
    lines.append("")
    lines.append("## Decision gate (per plan)")
    post = s["all_fills_post_haircut_mean_per_contract_cents"]
    wr = a["win_rate"]
    if post >= 1.0 and wr >= 0.55:
        lines.append("- **VERDICT: thesis confirmed (≥1c/contract post-haircut AND raw WR ≥55%)**")
    elif 0 <= post < 1.0:
        lines.append("- **VERDICT: marginal — defensive-only variant only**")
    else:
        lines.append("- **VERDICT: thesis fails (post-haircut < 0)**")
    lines.append("")
    lines.append("## Caveats and known issues")
    lines.append(f"- {s['fee_convention_note']}")
    lines.append("- Counterfactual is an upper bound; selection bias, slippage, and asymmetric")
    lines.append("  hit-rate haircut applied to derive the decision number.")
    lines.append("- Defensive subset uses volume-weighted entry price across net-side fills as a proxy.")
    lines.append("- Markets that have not yet resolved (or whose resolution lookup failed) are excluded.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-query SDK for all slugs (slower)")
    args = parser.parse_args()
    summary = run(db_path=args.db, out_dir=args.out, cache_path=args.cache,
                   force_refresh=args.force_refresh)
    print()
    print("=" * 70)
    print("PHASE 1.2 — COUNTERFACTUAL P&L SUMMARY")
    print("=" * 70)
    a = summary["all_fills_counterfactual"]
    print(f"Resolved fills: {summary['n_fills_in_resolved']} (contracts {a['n_contracts']})")
    print(f"Win rate: {a['win_rate']*100:.1f}%")
    print(f"Mean P&L: {a['mean_pnl_cents']:+.2f}c (post-haircut: "
          f"{summary['all_fills_post_haircut_mean_per_contract_cents']:+.2f}c)")
    print(f"Total counterfactual: ${a['total_pnl_cents']/100:+.2f}")
    print(f"Output: {args.out / 'settlement_pnl_report.md'}")


if __name__ == "__main__":
    main()
