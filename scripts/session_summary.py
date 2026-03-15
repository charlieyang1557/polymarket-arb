#!/usr/bin/env python3
"""
Generate a structured session summary from the MM paper trading DB.

Usage:
    python scripts/session_summary.py                    # latest session
    python scripts/session_summary.py --session-id XYZ   # specific session
    python scripts/session_summary.py --db data/mm_paper.db
"""

import argparse
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

    conn.close()

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
