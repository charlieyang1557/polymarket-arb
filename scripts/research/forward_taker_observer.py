#!/usr/bin/env python3
"""Experiment 2 — forward taker observer.

Reads the live trade tape (data/poly_trade_tape.db, captured by
scripts/trade_tape_collector.py since 2026-05-11) and computes the
hypothetical taker P&L if we'd taken the SAME direction as each
historical taker. This is the forward-looking counterpart to
taker_counterfactual.py, which only ran on the 326 historical maker
fills.

Rule (per trade in the tape):
  maker.side=BUY (a YES_BID maker was hit) → taker SOLD YES → us too,
    short YES at trade price. Settle: yes wins → (price-100); no → +price.
  maker.side=SELL (a YES_ASK maker was hit) → taker BOUGHT YES → us too,
    long YES at trade price. Settle: yes wins → (100-price); no → -price.

Taker fee subtracted at 0.02 × P(1-P) × 100 per contract (Polymarket sports).

The script optionally fetches settlement data via the Polymarket API
for slugs not in the cache, so the forward window naturally expands
as games complete.

Run:
    python scripts/research/forward_taker_observer.py
    python scripts/research/forward_taker_observer.py --fetch-settlements
    python scripts/research/forward_taker_observer.py --since 2026-05-15
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Allow importing src for poly_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

TRADE_TAPE_DB = "/Users/openclaw/polymarket-arb/data/poly_trade_tape.db"
SETTLEMENT_CACHE_RD = (
    "/Users/openclaw/polymarket-arb/.claude/worktrees/"
    "charming-mcclintock-caff3f/data/research/resolved_markets_cache.json"
)
# Writable cache for newly-fetched settlements (this worktree)
SETTLEMENT_CACHE_WR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "research", "forward_settlements.json"
)


# ---------------------------------------------------------------------------
# Pure functions (tested)
# ---------------------------------------------------------------------------

def _prefix7(slug: str) -> str:
    if not slug or "-" not in slug:
        return slug or ""
    parts = slug.split("-", 2)
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return slug


def _taker_fee(price_cents: int, quantity: int) -> float:
    """Polymarket sports taker fee per contract × quantity."""
    p = price_cents / 100
    return 0.02 * p * (1 - p) * 100 * quantity


def hypothetical_taker_pnl_settled(trade: dict, outcome: str | None) -> float:
    """Settlement P&L (no fee) for the hypothetical taker action.

    Returns 0 if outcome is None or the trade's sides don't correspond to a
    well-formed maker/taker pair.
    """
    if outcome is None:
        return 0.0
    maker_side = trade.get("maker_side", "")
    taker_side = trade.get("taker_side", "")
    price = trade["price_cents"]
    qty = trade["quantity"]
    is_buy = "BUY" in maker_side
    is_sell = "SELL" in maker_side
    if is_buy and is_sell:
        return 0.0
    # maker.side=BUY: taker SOLD YES; we'd short YES at price
    if "BUY" in maker_side and "SELL" in taker_side:
        per_contract = (price - 100) if outcome == "yes" else price
        return per_contract * qty
    # maker.side=SELL: taker BOUGHT YES; we'd long YES at price
    if "SELL" in maker_side and "BUY" in taker_side:
        per_contract = (100 - price) if outcome == "yes" else -price
        return per_contract * qty
    # Sides don't form a buy+sell pair → ambiguous, contribute 0
    return 0.0


def aggregate_forward_taker_per_prefix(trades: list, outcomes: dict) -> dict:
    """Aggregate hypothetical taker P&L per prefix7.

    Returns dict[prefix7] -> {
        n_trades, n_settled_trades, n_unsettled, n_contracts,
        settled_pnl_c, taker_fees_c, net_pnl_c,
        by_slug: dict[slug] -> {settled_pnl_c, n_settled, ...}
    }
    """
    agg: dict = defaultdict(lambda: {
        "n_trades": 0, "n_settled_trades": 0, "n_unsettled": 0,
        "n_contracts": 0, "settled_contracts": 0,
        "settled_pnl_c": 0.0, "taker_fees_c": 0.0, "net_pnl_c": 0.0,
        "by_slug": defaultdict(lambda: {
            "n_trades": 0, "n_settled": 0, "settled_pnl_c": 0.0,
        }),
    })
    for t in trades:
        slug = t["market_slug"]
        p = _prefix7(slug)
        b = agg[p]
        b["n_trades"] += 1
        b["n_contracts"] += t["quantity"]
        sb = b["by_slug"][slug]
        sb["n_trades"] += 1
        outcome = outcomes.get(slug)
        if outcome is None:
            b["n_unsettled"] += 1
            continue
        s = hypothetical_taker_pnl_settled(t, outcome)
        fee = _taker_fee(t["price_cents"], t["quantity"])
        b["n_settled_trades"] += 1
        b["settled_contracts"] += t["quantity"]
        b["settled_pnl_c"] += s
        b["taker_fees_c"] += fee
        b["net_pnl_c"] += (s - fee)
        sb["n_settled"] += 1
        sb["settled_pnl_c"] += s
    # Convert nested defaultdict for clean JSON
    return {p: {**v, "by_slug": dict(v["by_slug"])} for p, v in agg.items()}


# ---------------------------------------------------------------------------
# Data loading + settlement fetching
# ---------------------------------------------------------------------------

def load_trades(db_path: str, since: str | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    where = "1=1"
    params: list = []
    if since:
        where += " AND recorded_at >= ?"
        params.append(since)
    rows = conn.execute(f"""
        SELECT market_slug, price_cents, quantity, trade_time,
               maker_side, taker_side
        FROM mm_trade_tape
        WHERE {where}
    """, params).fetchall()
    conn.close()
    return [{"market_slug": r[0], "price_cents": r[1], "quantity": r[2],
             "trade_time": r[3], "maker_side": r[4], "taker_side": r[5]}
            for r in rows]


def load_outcomes(*paths) -> dict:
    outcomes: dict = {}
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path) as f:
            try:
                cache = json.load(f)
            except json.JSONDecodeError:
                continue
        for slug, info in cache.items():
            if not isinstance(info, dict):
                continue
            # New schema (forward_settlements.json): "settled" + "result"
            if info.get("settled") and info.get("result"):
                outcomes[slug] = info["result"]
                continue
            # Legacy schema (charming-mcclintock cache)
            for key in ("winning_outcome", "outcome", "resolution", "result"):
                v = info.get(key)
                if v:
                    outcomes[slug] = "yes" if "yes" in str(v).lower() else "no"
                    break
    return outcomes


def _extract_settlement(market: dict) -> dict:
    """Extract settlement info from a get_market response's 'market' dict.

    Polymarket US API doesn't have a clean `settled`/`result` field. The
    correct signals:
      ep3Status == 'EXPIRED': market has resolved (long-side price is final)
      marketSides[long=True].price == '1' → outcome=YES (long side won)
      marketSides[long=True].price == '0' → outcome=NO  (long side lost)
      Anything else (in-flight, ambiguous, '0.5' for tie) → not settled.

    Returns dict with settled (bool), result ('yes'|'no'|None),
    long_price (str), ep3_status (str), closed (bool).
    """
    ep3 = (market.get("ep3Status") or "").upper()
    closed = bool(market.get("closed", False))
    long_price = None
    for ms in market.get("marketSides", []) or []:
        if ms.get("long") is True:
            long_price = ms.get("price")
            break
    settled = ep3 == "EXPIRED" and long_price in ("0", "1")
    if settled:
        result = "yes" if long_price == "1" else "no"
    else:
        result = None
    return {
        "settled": settled,
        "result": result,
        "long_price": long_price,
        "ep3_status": ep3,
        "closed": closed,
    }


def fetch_settlements_for_slugs(slugs: list[str], cache_path: str) -> dict:
    """Fetch market resolution for each slug via poly_client.get_market.
    Writes results to cache_path. Re-fetches slugs not yet settled."""
    # Load existing cache
    existing: dict = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = {}
    to_fetch = [s for s in slugs if s not in existing or not existing[s].get("settled")]
    if not to_fetch:
        print(f"  All {len(slugs)} slugs already in cache as settled, no fetch needed")
        return existing

    print(f"  Fetching {len(to_fetch)} unresolved-or-missing slugs from Polymarket...")
    from dotenv import load_dotenv
    load_dotenv()
    from src.poly_client import PolyClient
    client = PolyClient()

    fetched = 0
    settled_now = 0
    for slug in to_fetch:
        try:
            resp = client.get_market(slug)
            m = resp.get("market", {})
            settlement = _extract_settlement(m)
            entry = {
                "slug": slug,
                "settled": settlement["settled"],
                "result": settlement["result"],
                "long_price": settlement["long_price"],
                "ep3_status": settlement["ep3_status"],
                "closed": settlement["closed"],
                "endDate": m.get("endDate") or "",
                "looked_up_at": datetime.utcnow().isoformat() + "Z",
            }
            existing[slug] = entry
            fetched += 1
            if entry["settled"]:
                settled_now += 1
        except Exception as e:
            print(f"  WARN: get_market failed for {slug}: {e}")

    # Write cache
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(existing, f, indent=2, sort_keys=True)
    print(f"  Fetched {fetched} markets, {settled_now} are now settled")
    print(f"  Cache: {cache_path}")
    return existing


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(agg: dict, outcomes: dict):
    total_trades = sum(v["n_trades"] for v in agg.values())
    total_settled = sum(v["n_settled_trades"] for v in agg.values())
    total_unsettled = sum(v["n_unsettled"] for v in agg.values())
    total_contracts = sum(v["n_contracts"] for v in agg.values())
    total_pnl = sum(v["settled_pnl_c"] for v in agg.values())
    total_fees = sum(v["taker_fees_c"] for v in agg.values())
    total_net = sum(v["net_pnl_c"] for v in agg.values())

    print()
    print("=" * 92)
    print("FORWARD TAKER OBSERVER — hypothetical taker P&L on trade tape since 2026-05-11")
    print("=" * 92)
    print(f"Total trades:     {total_trades:>5d}")
    print(f"  settled:        {total_settled:>5d}  ({total_settled/max(1,total_trades)*100:.0f}%)")
    print(f"  unsettled:      {total_unsettled:>5d}  (markets not yet resolved)")
    print(f"Total contracts:  {total_contracts:>5d}")
    print()
    print(f"{'prefix7':<14s} {'trades':>6s} {'settled':>8s} {'cntx':>6s} "
          f"{'settled $':>10s} {'fees $':>9s} {'net $':>10s} {'verdict':>20s}")
    print("-" * 92)
    rows = sorted(agg.items(),
                  key=lambda kv: -(kv[1]["settled_pnl_c"]))
    for p, v in rows:
        if v["n_settled_trades"] == 0:
            verdict = f"({v['n_unsettled']} unsettled)"
        elif v["net_pnl_c"] > 100:
            verdict = "POSITIVE"
        elif v["net_pnl_c"] < -100:
            verdict = "NEGATIVE"
        else:
            verdict = "near zero"
        print(f"{p:<14s} {v['n_trades']:>6d} {v['n_settled_trades']:>8d} "
              f"{v['settled_contracts']:>6d} "
              f"${v['settled_pnl_c']/100:>+8.2f} "
              f"${v['taker_fees_c']/100:>+7.2f} "
              f"${v['net_pnl_c']/100:>+8.2f} "
              f"{verdict:>20s}")
    print("-" * 92)
    print(f"{'TOTAL':<14s} {total_trades:>6d} {total_settled:>8d} "
          f"{sum(v['settled_contracts'] for v in agg.values()):>6d} "
          f"${total_pnl/100:>+8.2f} "
          f"${total_fees/100:>+7.2f} "
          f"${total_net/100:>+8.2f}")
    print()
    if total_settled == 0:
        print("⚠️  No settled trades yet. Re-run with --fetch-settlements once games complete.")
    else:
        print(f"Aggregate forward taker net P&L (after fees): ${total_net/100:+.2f}")
        print(f"Per-settled-trade average: {total_net/total_settled:+.2f}c")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=TRADE_TAPE_DB)
    parser.add_argument("--since", default=None,
                        help="Only consider trades recorded_at >= this date (UTC)")
    parser.add_argument("--fetch-settlements", action="store_true",
                        help="Call Polymarket API to fetch settlement status "
                             "for unresolved slugs. Writes to forward_settlements.json")
    parser.add_argument("--cache-rd", default=SETTLEMENT_CACHE_RD,
                        help="Read-only settlement cache (charming-mcclintock)")
    parser.add_argument("--cache-wr", default=SETTLEMENT_CACHE_WR,
                        help="Writable settlement cache for new fetches")
    args = parser.parse_args()

    trades = load_trades(args.db, args.since)
    print(f"Loaded {len(trades)} trades from {args.db}")
    if args.since:
        print(f"  (since {args.since})")

    if args.fetch_settlements:
        unique_slugs = sorted({t["market_slug"] for t in trades})
        fetch_settlements_for_slugs(unique_slugs, args.cache_wr)

    outcomes = load_outcomes(args.cache_rd, args.cache_wr)
    print(f"Loaded {len(outcomes)} resolved outcomes "
          f"(from {args.cache_rd} + {args.cache_wr})")

    agg = aggregate_forward_taker_per_prefix(trades, outcomes)
    print_report(agg, outcomes)


if __name__ == "__main__":
    main()
