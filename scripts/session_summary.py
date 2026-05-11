#!/usr/bin/env python3
"""
Generate a structured session summary from the MM paper trading DB.

Usage:
    python scripts/session_summary.py                    # latest session
    python scripts/session_summary.py --session-id XYZ   # specific session
    python scripts/session_summary.py --db data/mm_paper.db
"""

import argparse
import math
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SESSIONS_DIR = Path(".claude/sessions")


def get_session_id(conn: sqlite3.Connection, session_id: str | None) -> str:
    """Get session_id: use provided or find the latest one."""
    if session_id:
        return session_id
    row = conn.execute(
        "SELECT DISTINCT session_id FROM mm_fills "
        "ORDER BY filled_at DESC LIMIT 1").fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT DISTINCT session_id FROM mm_snapshots "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT DISTINCT session_id FROM mm_events "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    return row[0] if row else ""


def compute_per_side_stats(conn: sqlite3.Connection,
                           session_id: str) -> dict:
    """Aggregate yes_bid vs no_bid fill stats for the given session.

    The production diagnosis (fill_asymmetry_diagnosis.md) found 209
    yes_bid vs 117 no_bid fills — a 1.79x imbalance driven by structural
    YES-seller taker flow. This function surfaces that imbalance so it's
    visible in every session summary.

    Returns a dict:
      {
        "yes_bid": {n_fills, contracts, mean_price, paired_fills,
                    paired_contracts, win_rate, pair_pnl_sum, fees_sum},
        "no_bid":  same shape,
        "yes_no_ratio": float — n_yes_fills / n_no_fills; math.inf if
                                no NO fills; 0.0 if no fills at all.
      }
    win_rate is None when paired_fills == 0.
    """
    def _stats_for_side(side_value: str) -> dict:
        # Access by index to be agnostic to conn.row_factory setting
        row = conn.execute(
            "SELECT COUNT(*), "
            "       COALESCE(SUM(size), 0), "
            "       COALESCE(SUM(price*size)*1.0 / NULLIF(SUM(size),0), 0.0), "
            "       COALESCE(SUM(fee), 0.0) "
            "FROM mm_fills "
            "WHERE session_id = ? AND side = ?",
            (session_id, side_value)).fetchone()
        n_fills = row[0] or 0
        contracts = row[1] or 0
        mean_price = row[2] or 0.0
        fees_sum = row[3] or 0.0

        paired_row = conn.execute(
            "SELECT COUNT(*), "
            "       COALESCE(SUM(size), 0), "
            "       COALESCE(SUM(pair_pnl), 0.0), "
            "       SUM(CASE WHEN pair_pnl > 0 THEN 1 ELSE 0 END) "
            "FROM mm_fills "
            "WHERE session_id = ? AND side = ? AND pair_id IS NOT NULL",
            (session_id, side_value)).fetchone()
        n_paired = paired_row[0] or 0
        paired_contracts = paired_row[1] or 0
        pair_pnl_sum = paired_row[2] or 0.0
        n_wins = paired_row[3] or 0
        win_rate = (n_wins / n_paired) if n_paired > 0 else None

        return {
            "n_fills": n_fills,
            "contracts": contracts,
            "mean_price": float(mean_price),
            "paired_fills": n_paired,
            "paired_contracts": paired_contracts,
            "pair_pnl_sum": float(pair_pnl_sum),
            "win_rate": win_rate,
            "fees_sum": float(fees_sum),
        }

    yes = _stats_for_side("yes_bid")
    no = _stats_for_side("no_bid")

    if no["n_fills"] > 0:
        ratio = yes["n_fills"] / no["n_fills"]
    elif yes["n_fills"] > 0:
        ratio = math.inf
    else:
        ratio = 0.0

    return {"yes_bid": yes, "no_bid": no, "yes_no_ratio": ratio}


def _format_per_side_section(stats: dict) -> list[str]:
    """Render compute_per_side_stats output as markdown lines."""
    def _fmt_win_rate(wr):
        return f"{wr:.0%}" if wr is not None else "n/a"

    def _fmt_ratio(r):
        if r == math.inf:
            return "∞ (no_bid=0)"
        if r == 0.0:
            return "0.0"
        return f"{r:.2f}"

    y = stats["yes_bid"]
    n = stats["no_bid"]
    return [
        "## Per-Side Telemetry",
        "Surfaces yes_bid vs no_bid fill asymmetry — the primary driver of",
        "May 2026 losses. Target: ratio near 1.0 after Fix 1 (YES penalty).",
        "",
        "| Metric | yes_bid | no_bid |",
        "|---|---|---|",
        f"| Fills | {y['n_fills']} | {n['n_fills']} |",
        f"| Contracts | {y['contracts']} | {n['contracts']} |",
        f"| Mean price (c) | {y['mean_price']:.2f} | {n['mean_price']:.2f} |",
        f"| Paired fills | {y['paired_fills']} | {n['paired_fills']} |",
        f"| Paired contracts | {y['paired_contracts']} | {n['paired_contracts']} |",
        f"| Win rate (paired) | {_fmt_win_rate(y['win_rate'])} | {_fmt_win_rate(n['win_rate'])} |",
        f"| Pair P&L sum (c) | {y['pair_pnl_sum']:+.1f} | {n['pair_pnl_sum']:+.1f} |",
        f"| Fees sum (c) | {y['fees_sum']:+.2f} | {n['fees_sum']:+.2f} |",
        "",
        f"**yes_bid : no_bid fill ratio = {_fmt_ratio(stats['yes_no_ratio'])}**",
        "",
    ]


def compute_pnl_split(conn, session_id, ticker):
    """Decompose P&L into spread (paired round-trips) vs inventory (residual)."""
    fills = conn.execute(
        "SELECT side, price, size, fee FROM mm_fills "
        "WHERE session_id=? AND ticker=? AND side != 'settlement' "
        "ORDER BY filled_at",
        (session_id, ticker)).fetchall()

    yes_costs = []
    no_costs = []
    for row in fills:
        side, price, size, fee = row[0], row[1], row[2], row[3]
        per_fee = fee / size if size > 0 else 0
        if "yes" in side:
            yes_costs.extend([(price, per_fee)] * size)
        elif "no" in side:
            no_costs.extend([(price, per_fee)] * size)

    n_pairs = min(len(yes_costs), len(no_costs))
    spread_pnl = 0.0
    for i in range(n_pairs):
        yc, yf = yes_costs[i]
        nc, nf = no_costs[i]
        spread_pnl += 100 - yc - nc - yf - nf

    remaining_yes = len(yes_costs) - n_pairs
    remaining_no = len(no_costs) - n_pairs

    snap = conn.execute(
        "SELECT unrealized_pnl FROM mm_snapshots "
        "WHERE session_id=? AND ticker=? ORDER BY ts DESC LIMIT 1",
        (session_id, ticker)).fetchone()
    unrealized = snap[0] if snap else 0.0

    if remaining_yes > 0:
        leftover = yes_costs[n_pairs:]
        residual_side = "YES"
        residual_count = remaining_yes
        residual_avg = sum(p for p, _ in leftover) / len(leftover)
    elif remaining_no > 0:
        leftover = no_costs[n_pairs:]
        residual_side = "NO"
        residual_count = remaining_no
        residual_avg = sum(p for p, _ in leftover) / len(leftover)
    else:
        residual_side = None
        residual_count = 0
        residual_avg = 0

    return {
        "spread_pnl": round(spread_pnl, 1),
        "inventory_pnl": round(unrealized, 1),
        "round_trips": n_pairs,
        "residual_count": residual_count,
        "residual_side": residual_side,
        "residual_avg_cost": round(residual_avg, 0),
    }


def generate_summary(db_path: str, session_id: str | None = None) -> str:
    """Generate markdown summary from DB data."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sid = get_session_id(conn, session_id)
    if not sid:
        conn.close()
        return "# No session data found"

    # Duration from snapshots
    ts_row = conn.execute(
        "SELECT MIN(ts) as first_ts, MAX(ts) as last_ts "
        "FROM mm_snapshots WHERE session_id=?", (sid,)).fetchone()
    first_ts = ts_row["first_ts"] or "unknown"
    last_ts = ts_row["last_ts"] or "unknown"

    # Calculate duration
    duration_h = 0.0
    if first_ts != "unknown" and last_ts != "unknown":
        from datetime import datetime
        try:
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_h = (t1 - t0).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

    # Per-market stats
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM mm_fills WHERE session_id=? "
        "AND side != 'settlement'", (sid,)).fetchall()]
    if not tickers:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM mm_snapshots WHERE session_id=?",
            (sid,)).fetchall()]

    market_rows = []
    total_realized = 0.0
    total_fees = 0.0
    total_fills = 0
    total_roundtrips = 0
    queue_times = []

    for ticker in tickers:
        fills = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(size) as vol, SUM(fee) as fees "
            "FROM mm_fills WHERE session_id=? AND ticker=? "
            "AND side != 'settlement'",
            (sid, ticker)).fetchone()
        fill_count = fills["cnt"] or 0
        fees = fills["fees"] or 0.0

        # Pair P&L from settlement fills
        pnl_row = conn.execute(
            "SELECT SUM(pair_pnl) as pnl FROM mm_fills "
            "WHERE session_id=? AND ticker=? AND pair_pnl IS NOT NULL",
            (sid, ticker)).fetchone()
        realized_pnl = (pnl_row["pnl"] or 0.0) - fees

        # Round-trips: count settlement fills / 2
        settle_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM mm_fills "
            "WHERE session_id=? AND ticker=? AND side='settlement'",
            (sid, ticker)).fetchone()
        roundtrips = (settle_row["cnt"] or 0) // 2

        # Last snapshot for exit inventory
        last_snap = conn.execute(
            "SELECT net_inventory, realized_pnl FROM mm_snapshots "
            "WHERE session_id=? AND ticker=? ORDER BY ts DESC LIMIT 1",
            (sid, ticker)).fetchone()
        exit_inv = last_snap["net_inventory"] if last_snap else 0
        snap_pnl = last_snap["realized_pnl"] if last_snap else realized_pnl

        # Exit reason from events
        exit_ev = conn.execute(
            "SELECT action, trigger_reason FROM mm_events "
            "WHERE session_id=? AND ticker=? AND action='EXIT_MARKET' "
            "ORDER BY ts DESC LIMIT 1", (sid, ticker)).fetchone()
        exit_reason = exit_ev["trigger_reason"] if exit_ev else "active"

        # Queue times from filled orders
        qt_rows = conn.execute(
            "SELECT time_in_queue_s FROM mm_orders "
            "WHERE session_id=? AND ticker=? AND time_in_queue_s IS NOT NULL",
            (sid, ticker)).fetchall()
        for r in qt_rows:
            if r[0] is not None:
                queue_times.append(r[0])

        market_rows.append({
            "ticker": ticker,
            "fills": fill_count,
            "roundtrips": roundtrips,
            "realized_pnl": snap_pnl,
            "exit_inv": exit_inv,
            "exit_reason": exit_reason,
        })
        total_realized += snap_pnl
        total_fees += fees
        total_fills += fill_count
        total_roundtrips += roundtrips

    # Key events
    l3_events = conn.execute(
        "SELECT COUNT(*) as cnt FROM mm_events "
        "WHERE session_id=? AND layer=3", (sid,)).fetchone()["cnt"]
    l4_events = conn.execute(
        "SELECT COUNT(*) as cnt FROM mm_events "
        "WHERE session_id=? AND layer=4 AND action='PAUSE_60S'",
        (sid,)).fetchone()["cnt"]
    game_exits = conn.execute(
        "SELECT COUNT(*) as cnt FROM mm_events "
        "WHERE session_id=? AND trigger_reason LIKE '%GAME STARTED%'",
        (sid,)).fetchone()["cnt"]
    deactivations = conn.execute(
        "SELECT COUNT(*) as cnt FROM mm_events "
        "WHERE session_id=? AND action='EXIT_MARKET'",
        (sid,)).fetchone()["cnt"]

    # L3 reasons
    l3_reasons = conn.execute(
        "SELECT action, trigger_reason, COUNT(*) as cnt FROM mm_events "
        "WHERE session_id=? AND layer=3 GROUP BY action, trigger_reason",
        (sid,)).fetchall()

    # Build markdown
    avg_queue = (sum(queue_times) / len(queue_times)) if queue_times else 0

    lines = [
        f"# Session Summary: {sid}",
        f"Date: {first_ts[:10] if first_ts != 'unknown' else 'unknown'}",
        f"Duration: {duration_h:.1f}h",
        f"Markets: {len(tickers)}",
        "",
        "## Per-Market Results",
        "| Market | Fills | Round-trips | Realized P&L | Exit Inv | Exit Reason |",
        "|--------|-------|-------------|-------------|----------|-------------|",
    ]
    for m in market_rows:
        lines.append(
            f"| {m['ticker']} | {m['fills']} | {m['roundtrips']} | "
            f"{m['realized_pnl']:.1f}c | {m['exit_inv']} | {m['exit_reason']} |")

    lines.extend([
        "",
        "## Aggregate Stats",
        f"- Total realized P&L: {total_realized:.1f}c",
        f"- Total fees: {total_fees:.1f}c",
        f"- Total fills: {total_fills}",
        f"- Total round-trips: {total_roundtrips}",
        f"- Avg queue time to fill: {avg_queue:.0f}s",
        "",
        "## Key Events",
        f"- L3 triggers: {l3_events}",
    ])
    for r in l3_reasons:
        lines.append(f"  - {r['action']}: {r['trigger_reason']} (x{r['cnt']})")
    lines.extend([
        f"- L4 pauses: {l4_events}",
        f"- Game exits: {game_exits}",
        f"- Market deactivations: {deactivations}",
        "",
    ])
    # Per-side telemetry (yes_bid vs no_bid)
    lines.extend(_format_per_side_section(
        compute_per_side_stats(conn, sid)))

    lines.extend([
        "## P&L Decomposition (Spread vs Inventory)",
        "| Market | Round-trips | Spread P&L | Residual | Inventory P&L | Mix |",
        "|--------|------------|------------|----------|--------------|-----|",
    ])
    total_spread = 0.0
    total_inv = 0.0
    for ticker in tickers:
        split = compute_pnl_split(conn, sid, ticker)
        total_spread += split["spread_pnl"]
        total_inv += split["inventory_pnl"]
        residual_str = (f"{split['residual_count']} {split['residual_side']} "
                        f"@ {split['residual_avg_cost']:.0f}c"
                        if split["residual_side"] else "flat")
        abs_total = abs(split["spread_pnl"]) + abs(split["inventory_pnl"])
        if abs_total > 0:
            pct = f"{split['spread_pnl'] / abs_total * 100:.0f}%/{split['inventory_pnl'] / abs_total * 100:.0f}%"
        else:
            pct = "n/a"
        lines.append(
            f"| {ticker} | {split['round_trips']} | "
            f"{split['spread_pnl']:+.1f}c | {residual_str} | "
            f"{split['inventory_pnl']:+.1f}c | {pct} |")
    abs_grand = abs(total_spread) + abs(total_inv)
    if abs_grand > 0:
        grand_pct = f"{total_spread / abs_grand * 100:.0f}% spread / {total_inv / abs_grand * 100:.0f}% inv"
    else:
        grand_pct = "n/a"
    lines.append(f"| **Total** | | **{total_spread:+.1f}c** | | **{total_inv:+.1f}c** | {grand_pct} |")

    conn.close()

    lines.extend([
        "",
        "## What Worked",
        "<!-- Fill in manually or auto-detect -->",
        "",
        "## What Failed",
        "<!-- Fill in manually or auto-detect -->",
        "",
        "## Action Items for Next Session",
        "<!-- Fill in manually -->",
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate session summary")
    parser.add_argument("--db", default="data/mm_paper.db")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--output-dir", default=str(SESSIONS_DIR))
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}")
        sys.exit(1)

    summary = generate_summary(args.db, args.session_id)

    # Write to sessions dir
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Extract session_id for filename
    conn = sqlite3.connect(args.db)
    sid = get_session_id(conn, args.session_id)
    conn.close()

    filename = f"{sid}.md"
    output_path = output_dir / filename
    output_path.write_text(summary)
    print(f"Session summary written to {output_path}")
    print(summary)


if __name__ == "__main__":
    main()
