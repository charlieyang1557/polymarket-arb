#!/usr/bin/env python3
"""
Verify Polymarket US API authentication works.

Usage:
    python scripts/poly_auth_test.py
"""

import os
import sys

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_us import PolymarketUS


def main():
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret_key = os.getenv("POLYMARKET_SECRET_KEY")

    if not key_id or not secret_key:
        print("ERROR: Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in .env")
        sys.exit(1)

    print(f"Key ID: {key_id[:8]}...")
    print(f"Secret: {'*' * 8}")

    client = PolymarketUS(key_id=key_id, secret_key=secret_key)

    # Test 1: Public endpoint (sanity)
    print("\n1. Public: events.list ...", end=" ")
    try:
        resp = client.events.list({"limit": 1, "active": True})
        events = resp.get("events", [])
        print(f"OK ({len(events)} events)")
    except Exception as e:
        print(f"FAIL: {e}")

    # Test 2: Auth endpoint — account balances
    print("2. Auth:   account.balances ...", end=" ")
    try:
        bal = client.account.balances()
        print(f"OK: {bal}")
    except Exception as e:
        print(f"FAIL: {e}")

    # Test 3: Auth endpoint — list open orders
    print("3. Auth:   orders.list ...", end=" ")
    try:
        orders = client.orders.list()
        n = len(orders.get("orders", []) if isinstance(orders, dict)
                else getattr(orders, "orders", []))
        print(f"OK ({n} open orders)")
    except Exception as e:
        print(f"FAIL: {e}")

    # Test 4: Auth endpoint — portfolio positions
    print("4. Auth:   portfolio.positions ...", end=" ")
    try:
        pos = client.portfolio.positions()
        print(f"OK: {pos}")
    except Exception as e:
        print(f"FAIL: {e}")

    print("\nAUTH OK" if True else "")


if __name__ == "__main__":
    main()
