#!/usr/bin/env python3
"""Aggressor flow analysis on the trade tape.

Reads data/poly_trade_tape.db, decomposes trades by (prefix, maker.side,
taker.side, taker.intent), and reports:

  1. maker.side BUY:SELL ratio per prefix7 (= YES_BID drainage vs
     YES_ASK drainage; the empirical test of the structural asymmetry
     hypothesis from fill_asymmetry_diagnosis.md).

  2. Taker intent decomposition per prefix7 (open vs close;
     long-establishment vs short-establishment).

  3. Cross-reference against historical bot fills (poly_mm_live.db).

Run:
    python scripts/research/trade_tape_aggressor_analysis.py

Optional:
    --since YYYY-MM-DD     Filter trades after this date (UTC)
    --min-trades N         Skip prefixes with fewer than N trades
    --exclude-non-sports   Drop tc-temp-* and similar weather markets
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict

DB_PATH = "/Users/openclaw/polymarket-arb/data/poly_trade_tape.db"

# Prefixes that are not sports / not in the bot's intended scope.
# Bot has been incorrectly trading some of these — scanner-side bug.
NON_SPORTS_PREFIX7 = {"tc-temp"}


def load_trades(db_path: str, since: str | None):
    conn = sqlite3.connect(db_path)
    where = "1=1"
    params: list = []
    if since:
        where += " AND recorded_at >= ?"
        params.append(since)
    rows = conn.execute(f"""
        SELECT market_slug, price_cents, quantity, trade_time,
               maker_side, maker_intent, taker_side, taker_intent
        FROM mm_trade_tape
        WHERE {where}
    """, params).fetchall()
    conn.close()
    return [{"slug": r[0], "price_cents": r[1], "quantity": r[2],
             "trade_time": r[3], "maker_side": r[4], "maker_intent": r[5],
             "taker_side": r[6], "taker_intent": r[7]}
            for r in rows]


def prefix7(slug: str) -> str:
    """Return first 7 chars of slug (covers tsc-mlb / aec-nhl / atc-mls etc).

    Falls back to first chunk before '-' for shorter slugs.
    """
    if len(slug) >= 7 and "-" in slug:
        # First two dash-delimited components
        parts = slug.split("-")
        return "-".join(parts[:2])
    return slug


def short_side(s: str) -> str:
    if s == "ORDER_SIDE_BUY":
        return "BUY"
    if s == "ORDER_SIDE_SELL":
        return "SELL"
    return s[:8] if s else "?"


def short_intent(i: str) -> str:
    if not i:
        return "?"
    # ORDER_INTENT_BUY_LONG → BUY_LONG
    return i.replace("ORDER_INTENT_", "")


def aggressor_summary(trades, label: str):
    """Per-prefix maker.side BUY:SELL ratio + taker intent breakdown."""
    by_prefix: dict[str, dict] = defaultdict(lambda: {
        "n_trades": 0, "qty_total": 0,
        "maker_buy": 0, "maker_sell": 0, "maker_other": 0,
        "taker_buy_long": 0, "taker_buy_short": 0,
        "taker_sell_long": 0, "taker_sell_short": 0,
        "taker_other": 0,
    })
    for t in trades:
        p = prefix7(t["slug"])
        b = by_prefix[p]
        b["n_trades"] += 1
        b["qty_total"] += t["quantity"]
        ms = t["maker_side"]
        if ms == "ORDER_SIDE_BUY":
            b["maker_buy"] += 1
        elif ms == "ORDER_SIDE_SELL":
            b["maker_sell"] += 1
        else:
            b["maker_other"] += 1
        ti = t["taker_intent"]
        ts = t["taker_side"]
        if ti == "ORDER_INTENT_BUY_LONG":
            b["taker_buy_long"] += 1
        elif ti == "ORDER_INTENT_BUY_SHORT":
            b["taker_buy_short"] += 1
        elif ti == "ORDER_INTENT_SELL_LONG":
            b["taker_sell_long"] += 1
        elif ti == "ORDER_INTENT_SELL_SHORT":
            b["taker_sell_short"] += 1
        else:
            b["taker_other"] += 1

    print(f"\n{'=' * 76}")
    print(f"AGGRESSOR FLOW: {label}")
    print(f"{'=' * 76}")
    print(f"{'prefix7':<10s} {'trades':>6s} {'qty':>7s} "
          f"{'mBUY':>6s} {'mSEL':>6s} {'mB:mS':>7s}   "
          f"{'bL':>4s} {'bS':>4s} {'sL':>4s} {'sS':>4s}   "
          f"{'YES_BID':>8s} {'verdict':>14s}")
    print("-" * 76)
    rows = sorted(by_prefix.items(),
                  key=lambda kv: -kv[1]["n_trades"])
    for p, b in rows:
        if b["n_trades"] == 0:
            continue
        ratio = (b["maker_buy"] / b["maker_sell"]
                 if b["maker_sell"] > 0 else float("inf"))
        # YES_BID share = maker.side=BUY / (BUY + SELL). High share means
        # the YES_BID side of the orderbook was getting hit more often
        # than the YES_ASK side — confirms the diagnosis hypothesis.
        denom = b["maker_buy"] + b["maker_sell"]
        yes_bid_share = b["maker_buy"] / denom if denom else 0
        verdict = (
            "BAL"
            if 0.45 <= yes_bid_share <= 0.55
            else ("YES_BID heavy" if yes_bid_share > 0.55
                  else "NO_BID heavy")
        )
        print(f"{p:<10s} {b['n_trades']:>6d} {b['qty_total']:>7d} "
              f"{b['maker_buy']:>6d} {b['maker_sell']:>6d} "
              f"{ratio:>7.2f}   "
              f"{b['taker_buy_long']:>4d} {b['taker_buy_short']:>4d} "
              f"{b['taker_sell_long']:>4d} {b['taker_sell_short']:>4d}   "
              f"{yes_bid_share*100:>6.0f}%   {verdict:>14s}")
    print()
    print("Legend:")
    print("  mBUY/mSEL  : trades with maker.side=BUY (YES_BID hit) vs SELL (YES_ASK hit)")
    print("  mB:mS      : ratio. >1 means YES_BID hit more often (the historical signal)")
    print("  bL/bS/sL/sS: taker.intent decomposition (BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT)")
    print("  YES_BID    : maker.side=BUY share = % of trades that hit a YES_BID maker")
    print("  verdict    : BAL if 45%-55%, YES_BID heavy if >55%, NO_BID heavy if <45%")
    return by_prefix


def cross_reference_with_paper_fills():
    """Show paper bot fills last 7 days per prefix7 for comparison."""
    paper_db = "/Users/openclaw/polymarket-arb/data/poly_mm_paper.db"
    if not os.path.exists(paper_db):
        return
    conn = sqlite3.connect(paper_db)
    rows = conn.execute("""
        SELECT ticker, side, COUNT(*) AS n, SUM(size) AS qty
        FROM mm_fills
        WHERE filled_at > '2026-05-11'
          AND side IN ('yes_bid', 'no_bid')
        GROUP BY ticker, side
    """).fetchall()
    conn.close()
    by_pref = defaultdict(lambda: {"yes_bid": 0, "no_bid": 0,
                                    "yes_qty": 0, "no_qty": 0})
    for r in rows:
        p = prefix7(r[0])
        if r[1] == "yes_bid":
            by_pref[p]["yes_bid"] += r[2]
            by_pref[p]["yes_qty"] += r[3] or 0
        else:
            by_pref[p]["no_bid"] += r[2]
            by_pref[p]["no_qty"] += r[3] or 0
    print(f"\n{'=' * 76}")
    print("PAPER BOT FILLS (last 7 days, from poly_mm_paper.db) per prefix7")
    print(f"{'=' * 76}")
    print(f"{'prefix7':<10s} {'yes_bid':>9s} {'no_bid':>9s} "
          f"{'y:n ratio':>10s} {'yes_qty':>8s} {'no_qty':>8s}")
    print("-" * 60)
    for p, d in sorted(by_pref.items(),
                       key=lambda kv: -(kv[1]["yes_bid"] + kv[1]["no_bid"])):
        total = d["yes_bid"] + d["no_bid"]
        if total == 0:
            continue
        ratio = (d["yes_bid"] / d["no_bid"]
                 if d["no_bid"] > 0 else float("inf"))
        print(f"{p:<10s} {d['yes_bid']:>9d} {d['no_bid']:>9d} "
              f"{ratio:>10.2f} {d['yes_qty']:>8d} {d['no_qty']:>8d}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--since", default=None,
                        help="Filter trades recorded_at >= this date (UTC ISO)")
    parser.add_argument("--min-trades", type=int, default=10,
                        help="Skip prefixes with fewer than N trades")
    parser.add_argument("--exclude-non-sports", action="store_true",
                        help="Drop tc-temp-* and similar weather markets")
    args = parser.parse_args()

    trades = load_trades(args.db, args.since)
    print(f"Loaded {len(trades)} trades from {args.db}")
    if args.since:
        print(f"  (since {args.since})")

    by_pref_all = aggressor_summary(trades, "ALL trades")

    if args.exclude_non_sports:
        sports_trades = [t for t in trades
                         if prefix7(t["slug"]) not in NON_SPORTS_PREFIX7]
        if len(sports_trades) != len(trades):
            print(f"\nDropping {len(trades) - len(sports_trades)} non-sports trades")
            aggressor_summary(sports_trades, "Sports only (excl. tc-temp)")

    cross_reference_with_paper_fills()

    # Compute the aggregate YES_BID share for "core" sport prefixes
    core_prefixes = {"tsc-mlb", "tsc-nba", "tsc-nhl", "aec-mlb", "aec-nhl",
                     "aec-wnb", "aec-wta", "aec-atp", "asc-nba", "asc-mlb",
                     "atc-lal", "atc-mls", "astatc-"}
    core_buy = sum(by_pref_all[p]["maker_buy"] for p in core_prefixes
                   if p in by_pref_all)
    core_sell = sum(by_pref_all[p]["maker_sell"] for p in core_prefixes
                    if p in by_pref_all)
    if core_sell > 0:
        share = core_buy / (core_buy + core_sell)
        print(f"\n{'=' * 76}")
        print("AGGREGATE (core sport prefixes only):")
        print(f"  maker.side BUY (YES_BID drain): {core_buy}")
        print(f"  maker.side SELL (YES_ASK drain): {core_sell}")
        print(f"  ratio: {core_buy / core_sell:.2f}")
        print(f"  YES_BID share: {share*100:.1f}%")
        print(f"{'=' * 76}")


if __name__ == "__main__":
    main()
