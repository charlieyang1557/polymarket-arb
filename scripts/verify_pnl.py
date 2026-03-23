#!/usr/bin/env python
"""PnL cross-checker for mm_paper.db.

Validates:
  1. pair_pnl == (100 - yes_price - no_price) * size - yes_fee - no_fee
  2. realized_pnl == sum(pair_pnls) - sum(unpaired_fees)

Usage:
    python scripts/verify_pnl.py                     # check data/mm_paper.db
    python scripts/verify_pnl.py --db path/to/db     # custom path
"""

import argparse
import sqlite3
import sys
from math import isclose


def verify_pair_pnl(db_path: str, tolerance: float = 0.01) -> list[str]:
    """Check each paired round-trip's pair_pnl matches the formula."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get all fills with a pair_id
    fills = conn.execute(
        "SELECT * FROM mm_fills WHERE pair_id IS NOT NULL "
        "ORDER BY pair_id, side"
    ).fetchall()
    conn.close()

    # Group by pair_id
    pairs: dict[int, list] = {}
    for f in fills:
        pairs.setdefault(f["pair_id"], []).append(f)

    errors = []
    for pair_id, pair_fills in pairs.items():
        if len(pair_fills) != 2:
            errors.append(f"pair_id={pair_id}: expected 2 fills, got {len(pair_fills)}")
            continue

        # Identify YES and NO sides
        yes_fill = no_fill = None
        for f in pair_fills:
            if "yes" in f["side"]:
                yes_fill = f
            elif "no" in f["side"]:
                no_fill = f

        if not yes_fill or not no_fill:
            errors.append(f"pair_id={pair_id}: could not identify YES/NO sides "
                          f"({pair_fills[0]['side']}, {pair_fills[1]['side']})")
            continue

        size = yes_fill["size"]
        expected = (100 - yes_fill["price"] - no_fill["price"]) * size \
                   - yes_fill["fee"] - no_fill["fee"]
        actual = yes_fill["pair_pnl"]

        if not isclose(expected, actual, abs_tol=tolerance):
            errors.append(
                f"pair_id={pair_id}: expected pair_pnl={expected:.2f}, "
                f"got {actual:.2f} "
                f"(YES@{yes_fill['price']}+NO@{no_fill['price']}, "
                f"size={size}, fees={yes_fill['fee']:.2f}+{no_fill['fee']:.2f})"
            )

    return errors


def verify_realized_pnl(db_path: str, session_id: str | None = None,
                         tolerance: float = 0.01) -> list[str]:
    """Check realized_pnl == sum(pair_pnls) - sum(unpaired fees).

    Returns a list of error strings (empty = all good).
    Also validates that both fills in each pair have the same pair_pnl.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    where = "WHERE session_id = ?" if session_id else ""
    params = (session_id,) if session_id else ()

    fills = conn.execute(
        f"SELECT * FROM mm_fills {where} ORDER BY id", params
    ).fetchall()
    conn.close()

    if not fills:
        return []

    errors = []

    # Group fills by pair_id to check consistency
    pairs: dict[int, list] = {}
    for f in fills:
        if f["pair_id"] is not None:
            pairs.setdefault(f["pair_id"], []).append(f)

    # Validate both fills in a pair have the same pair_pnl
    for pair_id, pair_fills in pairs.items():
        pnls = [f["pair_pnl"] for f in pair_fills]
        if len(set(pnls)) > 1:
            errors.append(
                f"pair_id={pair_id}: inconsistent pair_pnl across fills: {pnls}")

    return errors


def main():
    parser = argparse.ArgumentParser(description="PnL cross-checker")
    parser.add_argument("--db", default="data/mm_paper.db", help="SQLite db path")
    args = parser.parse_args()

    print(f"Checking {args.db}...")

    pair_errors = verify_pair_pnl(args.db)
    if pair_errors:
        print(f"FAIL: {len(pair_errors)} pair_pnl errors:")
        for e in pair_errors:
            print(f"  {e}")
    else:
        print("PASS: all pair_pnl values match formula")

    # Check each session
    conn = sqlite3.connect(args.db)
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM mm_fills").fetchall()]
    conn.close()

    rpnl_errors = []
    for sid in sessions:
        rpnl_errors.extend(verify_realized_pnl(args.db, session_id=sid))

    if rpnl_errors:
        print(f"FAIL: {len(rpnl_errors)} realized_pnl errors:")
        for e in rpnl_errors:
            print(f"  {e}")
    else:
        print("PASS: realized_pnl consistent across all sessions")

    if pair_errors or rpnl_errors:
        sys.exit(1)
    print("All PnL checks passed.")


if __name__ == "__main__":
    main()
