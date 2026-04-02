#!/usr/bin/env python3
"""
Live market maker for Polymarket US.

Reuses the Kalshi MM engine logic (OBI microprice, continuous skew, dynamic
spread, 4-layer risk) but places REAL limit orders via the Polymarket SDK.

Key differences from poly_paper_mm.py:
  - Real order placement via PolyClient (place_order, cancel_order)
  - Fill detection by polling open orders (cumQuantity changes)
  - Position sync on startup from exchange
  - --dry-run flag for order preview without submission
  - Max single order value: 5% of capital
  - Crash handler: cancel ALL open orders on exit

Usage:
    python scripts/poly_live_mm.py \\
        --slugs SLUG1,SLUG2 --capital 2500 --size 2 --interval 10
    python scripts/poly_live_mm.py --dry-run \\
        --slugs SLUG1,SLUG2 --capital 2500
"""

import argparse
import atexit
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.poly_client import PolyClient, calculate_maker_fee
import src.mm.state as _mm_state
from src.mm.state import (
    MarketState, GlobalState, SimOrder,
    obi_microprice, skewed_quotes, dynamic_spread,
    maker_fee_cents, unrealized_pnl_cents,
)
from src.mm.engine import (
    MMEngine, discord_notify, clamp_order_size, soft_close_exit_price,
    is_side_cooled_down, should_skip_side, pair_off_inventory,
)
from src.mm.risk import (
    Action, check_layer1, check_layer2, check_layer3, check_layer4,
    highest_priority, apply_pause_30min,
)
from src.mm.db import MMDatabase


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_REQUOTE_DELTA = 2  # cents — only cancel+replace if |new - current| >= this
                       # Preserves queue priority in tight-spread markets
WORST_CASE_PER_CONTRACT = 50
MAX_ACTIVE_MARKETS = 10
ACTIVE_SLUGS_PATH = "data/poly_active_slugs.json"
PENDING_MARKETS_PATH = "data/pending_poly_markets.json"
MM_VERSION = "v1: Polymarket US LIVE — OBI + skew + capital-aware risk"


# ---------------------------------------------------------------------------
# Pure helper functions (tested)
# ---------------------------------------------------------------------------

def clamp_price(price_cents: int) -> int:
    """Clamp price to valid Polymarket range [1, 99] cents.

    Prices at 0 ($0.00) or 100 ($1.00) are invalid limit orders.
    """
    return max(1, min(99, price_cents))


def should_requote(target_price: int, current_price: int) -> bool:
    """Whether to cancel+replace an order based on price difference.

    Only requotes if the new price differs by >= MIN_REQUOTE_DELTA cents.
    In tight-spread markets (1-2c), this preserves queue priority by
    avoiding unnecessary cancel+replace cycles.
    """
    return abs(target_price - current_price) >= MIN_REQUOTE_DELTA


def should_requote_or_force(target_price: int, current_price: int,
                            force_requote: bool = False) -> bool:
    """Like should_requote, but with a force override.

    force_requote=True bypasses MIN_REQUOTE_DELTA check. Used when:
    - SOFT_CLOSE or AGGRESS_FLATTEN mode (risk overrides queue priority)
    - Inventory changed (fill detected, skew shifted)
    """
    if force_requote:
        return target_price != current_price
    return should_requote(target_price, current_price)


def max_order_value_check(price_cents: int, count: int,
                          capital_cents: int) -> bool:
    """Check if order value is within 5% of capital limit."""
    order_value = price_cents * count
    max_value = capital_cents * 0.05
    return order_value <= max_value


def side_to_intent(side: str) -> str:
    """Convert 'yes'/'no' to SDK OrderIntent."""
    if side.lower() == "yes":
        return "ORDER_INTENT_BUY_LONG"
    return "ORDER_INTENT_BUY_SHORT"


def intent_to_side(intent: str) -> str:
    """Convert SDK OrderIntent to 'yes'/'no'."""
    if intent in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT"):
        return "yes"
    return "no"


def parse_open_orders(resp: dict) -> dict:
    """Parse SDK open orders response into nested map.

    Returns: {slug: {side: {order_id, price_cents, original_qty,
                            filled_qty, remaining_qty}}}
    """
    result: dict = {}
    for order in resp.get("orders", []):
        slug = order.get("marketSlug", "")
        intent = order.get("intent", "")
        side = intent_to_side(intent)

        price_val = order.get("price", {})
        if isinstance(price_val, dict):
            price_cents = round(float(price_val.get("value", "0")) * 100)
        else:
            price_cents = round(float(price_val or 0) * 100)

        info = {
            "order_id": order.get("id", ""),
            "price_cents": price_cents,
            "original_qty": int(order.get("quantity", 0)),
            "filled_qty": int(order.get("cumQuantity", 0)),
            "remaining_qty": int(order.get("leavesQuantity", 0)),
        }

        if slug not in result:
            result[slug] = {}
        result[slug][side] = info

    return result


def detect_fills(prev_info: dict | None, curr_info: dict) -> int:
    """Detect new fills by comparing cumQuantity between ticks.

    Returns number of new fills (0 if order was replaced or no prev).
    """
    if prev_info is None:
        return 0
    if prev_info["order_id"] != curr_info["order_id"]:
        return 0  # order was replaced — don't double-count
    new_fills = curr_info["filled_qty"] - prev_info["filled_qty"]
    return max(0, new_fills)


def parse_positions(resp: dict) -> dict:
    """Parse portfolio positions to {slug: net_position_int} map.

    Excludes zero positions.
    """
    result = {}
    positions = resp.get("positions", {})
    for slug, pos in positions.items():
        net = int(pos.get("netPosition", "0"))
        if net != 0:
            result[slug] = net
    return result


def compute_risk_params(capital_cents: int) -> dict:
    """Derive risk thresholds from capital (same as paper)."""
    max_inv = max(4, int(capital_cents * 0.20 / WORST_CASE_PER_CONTRACT))
    max_unhedged = max(2, int(capital_cents * 0.10 / WORST_CASE_PER_CONTRACT))
    aggress_thresh = max(2, int(max_inv * 0.8))
    return {
        "max_inventory": max_inv,
        "max_unhedged_exit": max_unhedged,
        "aggress_threshold": aggress_thresh,
    }


# ---------------------------------------------------------------------------
# Reused from poly_paper_mm.py
# ---------------------------------------------------------------------------

def write_active_slugs_file(slugs: list[str], session_id: str,
                             path: str = ACTIVE_SLUGS_PATH):
    """Write current active slugs to state file (atomic)."""
    data = {
        "session_id": session_id,
        "active_slugs": slugs,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "LIVE",
    }
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)


def consume_pending_markets(gs, pending_path: str = PENDING_MARKETS_PATH,
                             active_path: str = ACTIVE_SLUGS_PATH,
                             game_start_lookup=None) -> list[str]:
    """Consume pending hot-add file and add markets to GlobalState."""
    if not os.path.exists(pending_path):
        return []
    processing = pending_path + ".processing"
    try:
        os.rename(pending_path, processing)
    except OSError:
        return []
    try:
        with open(processing) as f:
            data = json.load(f)
        slugs = data.get("slugs", [])
    except (json.JSONDecodeError, TypeError):
        slugs = []
    finally:
        try:
            os.unlink(processing)
        except OSError:
            pass
    if not slugs:
        return []
    added = []
    active_count = sum(1 for ms in gs.markets.values() if ms.active)
    for slug in slugs:
        if slug in gs.markets:
            continue
        if active_count >= MAX_ACTIVE_MARKETS:
            break
        game_start = None
        if game_start_lookup:
            gst_str = game_start_lookup(slug)
            if gst_str:
                try:
                    game_start = datetime.fromisoformat(
                        gst_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
        gs.markets[slug] = MarketState(
            ticker=slug, game_start_utc=game_start)
        added.append(slug)
        active_count += 1
    if added:
        active_slugs = [s for s, ms in gs.markets.items() if ms.active]
        write_active_slugs_file(active_slugs, gs.session_id, active_path)
    return added


# ---------------------------------------------------------------------------
# Live order manager
# ---------------------------------------------------------------------------

class LiveOrderManager:
    """Manages real orders on Polymarket US.

    Each tick: compute quotes → compare to existing orders →
    cancel+replace if price changed beyond REQUOTE_TOL.

    Tracks fills by polling open orders and comparing cumQuantity.
    """

    def __init__(self, client: PolyClient, dry_run: bool = False,
                 capital_cents: int = 2500):
        self.client = client
        self.dry_run = dry_run
        self.capital_cents = capital_cents
        # Previous tick's order state: {slug: {side: order_info}}
        self._prev_orders: dict = {}
        # Track exchange order IDs we've placed: {slug: {side: order_id}}
        self._live_order_ids: dict = {}
        # Local order tracking: {slug: {side: {order_id, price_cents, ...}}}
        # Prevents requote thrashing when poll_open_orders returns empty
        self._local_orders: dict = {}
        # Track order IDs we explicitly cancelled — {order_id}
        # Used to distinguish "disappeared because filled" from "cancelled"
        self._cancelled_order_ids: set = set()
        self._api_backoff = 0  # exponential backoff counter for 429s

    def cancel_all_orders(self, slugs: list[str] | None = None):
        """Cancel ALL open orders. Called on startup, shutdown, crash.

        After cancelling, reconciles via open-orders poll to detect
        any fills that raced with the cancel request.
        """
        # Collect all tracked IDs (to mark after success)
        all_tracked_ids: set = set()
        for slug_sides in self._live_order_ids.values():
            for oid in slug_sides.values():
                all_tracked_ids.add(oid)
        for slug_sides in self._local_orders.values():
            for info in slug_sides.values():
                all_tracked_ids.add(info.get("order_id", ""))

        if self.dry_run:
            print("  [DRY-RUN] Would cancel all orders", flush=True)
            self._cancelled_order_ids.update(all_tracked_ids)
            self._live_order_ids.clear()
            self._local_orders.clear()
            return
        try:
            if slugs:
                resp = self.client.cancel_all_orders()
            else:
                resp = self.client.cancel_all_orders()
            cancelled = resp.get("canceledOrderIds", [])
            # Mark only confirmed cancelled IDs
            for oid in cancelled:
                self._cancelled_order_ids.add(oid)
            print(f"  Cancelled {len(cancelled)} open orders", flush=True)
        except Exception as e:
            print(f"  WARNING: cancel_all failed: {e}", file=sys.stderr,
                  flush=True)

        # Reconciliation: check if any orders survived the cancel
        remaining = self._reconcile_after_cancel()
        # IDs NOT in remaining were successfully cancelled
        remaining_ids = {
            info.get("order_id", "")
            for sides in remaining.values()
            for info in sides.values()
        }
        for oid in all_tracked_ids:
            if oid not in remaining_ids:
                self._cancelled_order_ids.add(oid)

        self._live_order_ids.clear()
        self._local_orders.clear()

    def _reconcile_after_cancel(self) -> dict:
        """Poll open orders after cancel to detect survivors.

        Returns remaining orders dict. Best-effort — returns {}
        on failure (startup position sync is the final safety net).
        """
        try:
            resp = self.client.list_orders(slugs=[])
            remaining = parse_open_orders(resp)
            if remaining:
                order_count = sum(
                    len(sides) for sides in remaining.values())
                print(f"  RECONCILE: {order_count} orders still open "
                      f"after cancel_all", flush=True)
            return remaining
        except Exception as e:
            print(f"  RECONCILE_ERROR: {e}", file=sys.stderr, flush=True)
            return {}

    def place_order(self, slug: str, side: str, price_cents: int,
                    count: int) -> str | None:
        """Place a limit order. Returns order ID or None.

        Safety checks:
        - Clamps price to [1, 99] cents (Polymarket rejects 0 and 100)
        - Max order value: 5% of capital
        - On timeout: assumes dirty state, verifies via open orders poll
        """
        # Strict price bounds
        price_cents = clamp_price(price_cents)

        # Safety: max order value check
        if not max_order_value_check(price_cents, count, self.capital_cents):
            print(f"    REJECT {slug} {side}@{price_cents}c x{count}: "
                  f"exceeds 5% capital limit", flush=True)
            return None

        if self.dry_run:
            print(f"    [DRY-RUN] Would place {side}@{price_cents}c "
                  f"x{count} on {slug}", flush=True)
            dry_id = f"dry-{uuid.uuid4().hex[:8]}"
            self._local_orders.setdefault(slug, {})[side] = {
                "order_id": dry_id,
                "price_cents": price_cents,
                "original_qty": count,
                "filled_qty": 0,
                "remaining_qty": count,
            }
            return dry_id

        try:
            resp = self.client.place_order(
                slug, side=side, price=price_cents, count=count)
            order_id = resp.get("id", "")
            if not order_id:
                order_id = resp.get("order", {}).get("id", "") if isinstance(
                    resp.get("order"), dict) else ""
            if order_id:
                if slug not in self._live_order_ids:
                    self._live_order_ids[slug] = {}
                self._live_order_ids[slug][side] = order_id
                self._local_orders.setdefault(slug, {})[side] = {
                    "order_id": order_id,
                    "price_cents": price_cents,
                    "original_qty": count,
                    "filled_qty": 0,
                    "remaining_qty": count,
                }
            self._api_backoff = 0
            print(f"    PLACED {slug} {side}@{price_cents}c x{count} id={order_id}", flush=True)
            return order_id
        except Exception as e:
            err_str = str(e)
            if "timeout" in err_str.lower() or "timed out" in err_str.lower():
                # Schrodinger state: order may or may not have been placed.
                # Do NOT retry blindly — verify exchange state first.
                print(f"    TIMEOUT placing {slug} {side}@{price_cents}c: "
                      f"verifying exchange state...", flush=True)
                try:
                    exchange_orders, _ = self.poll_open_orders([slug])
                    if slug in exchange_orders and side in exchange_orders[slug]:
                        oid = exchange_orders[slug][side]["order_id"]
                        print(f"    TIMEOUT RECOVERED: order {oid} exists "
                              f"on exchange", flush=True)
                        if slug not in self._live_order_ids:
                            self._live_order_ids[slug] = {}
                        self._live_order_ids[slug][side] = oid
                        self._local_orders.setdefault(slug, {})[side] = {
                            "order_id": oid,
                            "price_cents": price_cents,
                            "original_qty": count,
                            "filled_qty": 0,
                            "remaining_qty": count,
                        }
                        return oid
                    else:
                        print(f"    TIMEOUT: order did NOT reach exchange",
                              flush=True)
                except Exception as verify_err:
                    print(f"    TIMEOUT VERIFY FAILED: {verify_err}",
                          file=sys.stderr, flush=True)
                return None
            elif "429" in err_str or "rate" in err_str.lower():
                self._api_backoff = min(self._api_backoff + 1, 5)
                wait = 2 ** self._api_backoff
                print(f"    RATE LIMITED: backing off {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"    ORDER ERROR {slug} {side}: {e}",
                      file=sys.stderr, flush=True)
            return None

    def cancel_order(self, slug: str, side: str, order_id: str):
        """Cancel a specific order.

        Only marks order as cancelled AFTER confirmed success.
        Failed cancels leave the order trackable for fill detection.
        """
        if self.dry_run:
            self._cancelled_order_ids.add(order_id)
            print(f"    [DRY-RUN] Would cancel {side} order {order_id} "
                  f"on {slug}", flush=True)
            if slug in self._local_orders:
                self._local_orders[slug].pop(side, None)
            return

        try:
            self.client.cancel_order(order_id, slug=slug)
            # Only mark as cancelled after exchange confirms
            self._cancelled_order_ids.add(order_id)
            if slug in self._live_order_ids:
                self._live_order_ids[slug].pop(side, None)
            if slug in self._local_orders:
                self._local_orders[slug].pop(side, None)
            self._api_backoff = 0
        except Exception as e:
            # Do NOT mark as cancelled — cancel may not have reached exchange
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower():
                self._api_backoff = min(self._api_backoff + 1, 5)
                wait = 2 ** self._api_backoff
                print(f"    RATE LIMITED: backing off {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"    CANCEL ERROR {order_id}: {e}",
                      file=sys.stderr, flush=True)

    def poll_open_orders(self, slugs: list[str]) -> tuple[dict, bool]:
        """Fetch current open orders for active slugs.

        Returns (parsed_order_map, poll_succeeded).
        poll_succeeded=False means the data is stale — callers must not
        infer fills from missing orders.
        Remaps API marketSlug to our slug via order_id lookup in
        _live_order_ids, so slug format mismatches don't break tracking.
        """
        try:
            resp = self.client.list_orders(slugs=slugs)
            parsed = parse_open_orders(resp)

            # Build reverse map: order_id → our slug
            id_to_slug: dict[str, str] = {}
            for slug, sides in self._live_order_ids.items():
                for side, oid in sides.items():
                    id_to_slug[oid] = slug

            # Remap API slugs to our slugs
            fixed: dict = {}
            for api_slug, sides in parsed.items():
                for side, info in sides.items():
                    oid = info.get("order_id", "")
                    real_slug = id_to_slug.get(oid, api_slug)
                    fixed.setdefault(real_slug, {})[side] = info
            return fixed, True
        except Exception as e:
            print(f"    POLL ERROR: {e}", file=sys.stderr, flush=True)
            return {}, False

    def check_fills(self, curr_orders: dict) -> list[dict]:
        """Compare current orders to previous tick to detect fills.

        Returns list of fill events:
            [{"slug": ..., "side": ..., "filled": ..., "price_cents": ...}]

        Handles three cases:
        1. Order still visible: compare cumQuantity (partial fills)
        2. Order disappeared + was cancelled: not a fill
        3. Order disappeared + NOT cancelled: was filled (remaining qty)
        """
        fills = []
        for slug, sides in self._prev_orders.items():
            for side, prev_info in sides.items():
                curr_info = curr_orders.get(slug, {}).get(side)
                if curr_info is not None:
                    n = detect_fills(prev_info, curr_info)
                    if n > 0:
                        fills.append({
                            "slug": slug,
                            "side": side,
                            "filled": n,
                            "price_cents": prev_info["price_cents"],
                        })
                else:
                    # Order disappeared — check if it was cancelled
                    prev_oid = prev_info.get("order_id", "")
                    if prev_oid in self._cancelled_order_ids:
                        # Explicitly cancelled — not a fill
                        print(f"    CANCEL_CONFIRMED: {slug} {side} "
                              f"{prev_oid} (cancelled, not filled)",
                              flush=True)
                        self._cancelled_order_ids.discard(prev_oid)
                        continue

                    # Not cancelled → was filled. Count remaining qty.
                    remaining = prev_info["remaining_qty"]
                    if remaining > 0:
                        fills.append({
                            "slug": slug,
                            "side": side,
                            "filled": remaining,
                            "price_cents": prev_info["price_cents"],
                        })
                        # Clean up local tracking for filled order
                        if slug in self._local_orders:
                            self._local_orders[slug].pop(side, None)
                        if slug in self._live_order_ids:
                            self._live_order_ids[slug].pop(side, None)
        return fills

    def merged_orders(self, polled: dict, poll_ok: bool = True) -> dict:
        """Merge poll results with local tracking.

        When poll_ok=False (API error), returns previous tick's orders
        unchanged to prevent phantom fill detection.

        When poll_ok=True:
        - Exchange data wins for orders present in poll
        - Local data fills gaps only for freshly placed orders (not in
          _prev_orders). Previously tracked orders that disappear are
          left absent so check_fills() can detect fills.
        - Orders under mismatched slugs are matched by order_id to
          prevent phantom fills from slug remap failures.
        """
        if not poll_ok:
            # Stale data — return previous tick unchanged
            return dict(self._prev_orders)

        # Build order_id index from polled data for cross-slug matching.
        # This catches orders that appear under a different API slug
        # when _live_order_ids has lost the remap entry.
        polled_by_oid: dict[str, tuple[str, str, dict]] = {}
        for slug, sides in polled.items():
            for side, info in sides.items():
                oid = info.get("order_id", "")
                if oid:
                    polled_by_oid[oid] = (slug, side, info)

        merged = {}
        # Start with polled data
        for slug, sides in polled.items():
            merged[slug] = dict(sides)

        # Remap: for each prev_orders entry missing from polled,
        # check if the order_id exists under a different polled slug
        for slug, sides in self._prev_orders.items():
            if slug in merged:
                continue  # already present
            for side, prev_info in sides.items():
                prev_oid = prev_info.get("order_id", "")
                if prev_oid and prev_oid in polled_by_oid:
                    # Order exists but under a different slug — remap it
                    _, _, matched_info = polled_by_oid[prev_oid]
                    merged.setdefault(slug, {})[side] = matched_info
        # Fill gaps from local tracking — but only for orders not yet
        # seen in a previous poll (not in _prev_orders)
        for slug, sides in self._local_orders.items():
            if slug not in merged:
                merged[slug] = {}
            for side, info in sides.items():
                if side not in merged[slug]:
                    # Only fill from local if this order wasn't tracked
                    # in the previous tick. If it was tracked and now
                    # disappeared, it was likely filled.
                    was_tracked = (slug in self._prev_orders
                                   and side in self._prev_orders[slug])
                    if not was_tracked:
                        merged[slug][side] = dict(info)
                else:
                    # Exchange truth — update local tracking
                    self._local_orders[slug][side] = dict(merged[slug][side])
        return merged

    def update_prev_orders(self, curr_orders: dict):
        """Store current tick's orders for next fill comparison."""
        self._prev_orders = curr_orders

    def sync_positions(self, gs: GlobalState, slugs: list[str]):
        """Fetch real positions from exchange and sync to MarketState.

        Called on startup to reconcile local state with exchange truth.
        This is the final safety net: catches ANY missed fills from
        previous sessions, cancel/fill races, or detection gaps.
        """
        try:
            resp = self.client.get_positions()
            positions = parse_positions(resp)

            for slug in slugs:
                ms = gs.markets.get(slug)
                if ms is None:
                    continue

                net_pos = positions.get(slug, 0)
                # Overwrite local inventory with exchange truth
                ms.yes_queue.clear()
                ms.no_queue.clear()
                if net_pos > 0:
                    # Long position — approximate as YES buys at midpoint
                    ms.yes_queue.extend([50] * net_pos)
                elif net_pos < 0:
                    ms.no_queue.extend([50] * abs(net_pos))

                print(f"  Synced: {slug} inv={net_pos}", flush=True)
        except Exception as e:
            print(f"  WARNING: position sync failed: {e}",
                  file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Apply Polymarket fee patch (rebates instead of fees)
    _mm_state.maker_fee_cents = lambda price_cents, count=1: calculate_maker_fee(
        price_cents, category="sports", count=count)

    parser = argparse.ArgumentParser(
        description="LIVE market maker — Polymarket US")
    parser.add_argument("--slugs", required=True,
                        help="Comma-separated market slugs")
    parser.add_argument("--duration", type=int, default=86400,
                        help="Seconds to run (default: 24h)")
    parser.add_argument("--size", type=int, default=2,
                        help="Contracts per order (default: 2)")
    parser.add_argument("--interval", type=int, default=10,
                        help="Seconds between ticks per market (default: 10)")
    parser.add_argument("--capital", type=int, default=2500,
                        help="Capital in cents (default: 2500 = $25)")
    parser.add_argument("--db-path", default="data/poly_mm_live.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print orders without submitting")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip startup confirmation prompt")
    args = parser.parse_args()

    # Auth required for live trading
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret_key = os.getenv("POLYMARKET_SECRET_KEY")

    if not key_id or not secret_key:
        if args.dry_run:
            print("WARNING: No API keys — dry-run will use public data only",
                  flush=True)
            client = PolyClient()
        else:
            print("FATAL: POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY required "
                  "for live trading", file=sys.stderr)
            sys.exit(1)
    else:
        client = PolyClient(key_id=key_id, secret_key=secret_key)

    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + \
                 uuid.uuid4().hex[:6]

    live_mgr = LiveOrderManager(
        client, dry_run=args.dry_run, capital_cents=args.capital)

    # ---- Startup sequence ----

    # 1. Fetch real balance
    if not args.dry_run:
        try:
            bal_resp = client.get_balance()
            balances = bal_resp.get("balances", [bal_resp])
            if isinstance(balances, list) and balances:
                usdc_bal = balances[0].get("currentBalance", 0)
            elif isinstance(balances, dict):
                usdc_bal = balances.get("currentBalance", 0)
            else:
                usdc_bal = 0
            usdc_cents = int(float(usdc_bal) * 100)
            print(f"  USDC balance: ${usdc_bal:.2f} ({usdc_cents}c)")

            if usdc_cents < args.capital:
                print(f"FATAL: Balance {usdc_cents}c < capital {args.capital}c",
                      file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"FATAL: Cannot fetch balance: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        usdc_cents = args.capital  # dry-run assumes sufficient

    # 2. Cancel ALL existing open orders (orphan cleanup)
    print("  Cancelling orphan orders...", flush=True)
    live_mgr.cancel_all_orders(slugs)

    # 3. Initialize DB and GlobalState
    db = MMDatabase(args.db_path, session_id)
    gs = GlobalState(session_id=session_id)

    # 4. Load game start times
    schedule = {}
    targets_file = Path("data/polymarket_diagnostic/daily_targets.json")
    try:
        with open(targets_file) as f:
            for t in json.load(f):
                gst = t.get("game_start_time") or ""
                if gst and t.get("slug"):
                    schedule[t["slug"]] = gst
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    for slug in slugs:
        if slug not in schedule:
            try:
                raw = client.get_market(slug)
                market = raw.get("market", raw) or {}
                gst = market.get("gameStartTime") or ""
                if gst:
                    schedule[slug] = gst
            except Exception:
                pass

    # 5. Initialize markets
    for slug in slugs:
        game_start = None
        gst_str = schedule.get(slug)
        if gst_str:
            try:
                game_start = datetime.fromisoformat(
                    gst_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        gs.markets[slug] = MarketState(
            ticker=slug, game_start_utc=game_start)

    # 6. Sync positions from exchange
    print("  Syncing exchange positions...", flush=True)
    live_mgr.sync_positions(gs, slugs)

    risk = compute_risk_params(args.capital)

    # 7. Confirmation
    mode_str = "[DRY-RUN]" if args.dry_run else "LIVE MODE"
    print(f"\n{'='*70}")
    print(f"  {mode_str}: ${args.capital/100:.0f} capital, "
          f"${usdc_cents/100:.2f} USDC available")
    print(f"  {len(slugs)} markets | {args.size} contracts | "
          f"{args.interval}s interval")
    print(f"  MAX_INV: {risk['max_inventory']} | "
          f"UNHEDGED: {risk['max_unhedged_exit']} | "
          f"AGGRESS: {risk['aggress_threshold']}")
    print(f"  Session: {session_id}")
    print(f"{'='*70}")

    if not args.dry_run and not args.no_confirm:
        try:
            resp = input("\nPress Enter to start live trading (Ctrl+C to abort): ")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

    # Register crash handler: cancel all on exit
    def _crash_cleanup():
        print("\nEmergency cleanup: cancelling all orders...", flush=True)
        live_mgr.cancel_all_orders()

    atexit.register(_crash_cleanup)

    # Graceful shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        print("\nShutting down gracefully...", flush=True)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Track rebates
    rebates_earned = {slug: 0.0 for slug in slugs}

    discord_notify(
        f"**Poly {'DRY-RUN' if args.dry_run else 'LIVE'} MM Started** | "
        f"{len(slugs)} markets | session={session_id}\n"
        f"Slugs: {', '.join(slugs)}")

    write_active_slugs_file(slugs, session_id)

    active_slugs = list(slugs)
    start = time.time()
    cycle = 0
    last_summary_time = start
    SUMMARY_INTERVAL = 43200  # 12h
    tick_count = 0

    def _lookup_game_start(slug):
        try:
            raw = client.get_market(slug)
            market = raw.get("market", raw) or {}
            return market.get("gameStartTime") or ""
        except Exception:
            return ""

    try:
        while not shutdown and (time.time() - start) < args.duration:
            # Hot-add check
            added = consume_pending_markets(
                gs, game_start_lookup=_lookup_game_start)
            if added:
                for slug in added:
                    rebates_earned[slug] = 0.0
                discord_notify(
                    f"**Hot-added {len(added)} markets** | "
                    f"session={session_id}\n" + ", ".join(added))

            all_slugs = list(gs.markets.keys())
            active_slugs = [s for s in all_slugs if gs.markets[s].active]
            if not active_slugs:
                print("All markets inactive. Stopping.", flush=True)
                break

            sleep_time = args.interval / max(len(active_slugs), 1)

            # Poll open orders for all active slugs (one API call)
            raw_polled, poll_ok = live_mgr.poll_open_orders(active_slugs)
            curr_orders = live_mgr.merged_orders(raw_polled, poll_ok)

            # Detect fills from order state changes (only on clean poll)
            inv_changed_slugs: set = set()
            if poll_ok:
                fills = live_mgr.check_fills(curr_orders)
            else:
                fills = []
                print("    SKIP_FILL_CHECK: poll failed, using stale state",
                      flush=True)
            for f in fills:
                slug = f["slug"]
                ms = gs.markets.get(slug)
                if ms is None:
                    continue

                side = f["side"]
                filled = f["filled"]
                price = f["price_cents"]

                # Record fill in local state
                fee = maker_fee_cents(price, filled)
                ms.total_fees += fee
                ms.realized_pnl -= fee

                inv_changed_slugs.add(slug)

                if side == "yes":
                    ms.yes_queue.extend([price] * filled)
                else:
                    ms.no_queue.extend([price] * filled)

                if ms.oldest_fill_time is None:
                    ms.oldest_fill_time = datetime.now(timezone.utc)

                inv = ms.net_inventory
                rebate = abs(calculate_maker_fee(price, count=filled))
                rebates_earned[slug] = rebates_earned.get(slug, 0) + rebate

                print(f"  >>> FILL [MAKER] {slug} {side}_bid "
                      f"{filled}@{price}c fee={fee:.2f}c inv={inv} "
                      f"pnl={ms.realized_pnl:.1f}c", flush=True)
                discord_notify(
                    f"**{'DRY' if args.dry_run else 'LIVE'} MM Fill** "
                    f"{slug} {side}_bid {filled}@{price}c | inv={inv} | "
                    f"pnl={ms.realized_pnl:.1f}c")

                # DB
                try:
                    db.insert_fill(
                        order_id=None, ticker=slug, side=f"{side}_bid",
                        price=price, size=filled, fee=fee, is_taker=0,
                        inventory_after=inv,
                        filled_at=datetime.now(timezone.utc).isoformat())
                    gs.db_error_count = 0
                except Exception as e:
                    gs.db_error_count += 1

            # Pair off matched inventory for all active markets
            for slug in active_slugs:
                ms = gs.markets[slug]
                pairs = pair_off_inventory(ms)
                for p in pairs:
                    ms.realized_pnl += p["gross_pnl"]
                    if p["gross_pnl"] < 0:
                        ms.consecutive_losses += 1
                    else:
                        ms.consecutive_losses = 0
                if not ms.yes_queue and not ms.no_queue:
                    ms.oldest_fill_time = None
                    ms.skew_activated_at = None

            live_mgr.update_prev_orders(curr_orders)

            # Process each market
            for i, slug in enumerate(active_slugs):
                if shutdown:
                    break
                if cycle % len(active_slugs) != i:
                    continue

                ms = gs.markets[slug]
                now = datetime.now(timezone.utc)

                # Check pause
                if ms.paused_until and now < ms.paused_until:
                    continue

                # Fetch orderbook
                try:
                    book_data = client.get_orderbook(slug, depth=20)
                    ms.last_api_success = now
                except Exception as e:
                    continue

                book_fp = book_data.get("orderbook_fp", {})
                yes_bids_raw = book_fp.get("yes_dollars", [])
                no_bids_raw = book_fp.get("no_dollars", [])

                if not yes_bids_raw or not no_bids_raw:
                    ms.consecutive_skip_ticks += 1
                    if ms.consecutive_skip_ticks >= 30:
                        _cancel_market_orders(live_mgr, slug, curr_orders)
                        ms.active = False
                        ms.deactivation_reason = "orderbook_dead"
                        discord_notify(
                            f"**LIVE MM** orderbook dead: {slug}")
                    continue

                ms.consecutive_skip_ticks = 0

                yes_bids = [[round(float(p) * 100), int(float(q))]
                             for p, q in yes_bids_raw]
                no_bids = [[round(float(p) * 100), int(float(q))]
                            for p, q in no_bids_raw]

                best_yes_bid = yes_bids[-1][0]
                best_no_bid = no_bids[-1][0]
                yes_ask = 100 - best_no_bid
                spread = yes_ask - best_yes_bid
                yes_depth = sum(q for _, q in yes_bids)
                no_depth = sum(q for _, q in no_bids)
                midpoint = obi_microprice(best_yes_bid, yes_ask,
                                          yes_depth, no_depth)

                if ms.session_initial_midpoint is None:
                    ms.session_initial_midpoint = midpoint

                ms.midpoint_history.append((now, midpoint))
                if len(ms.midpoint_history) > 7:
                    ms.midpoint_history.pop(0)

                # Update unrealized
                ms.unrealized_pnl = unrealized_pnl_cents(
                    ms.yes_queue, ms.no_queue, best_yes_bid, best_no_bid)

                # Update peak
                total = gs.total_pnl
                if total > gs.peak_total_pnl:
                    gs.peak_total_pnl = total

                # Live game check
                if ms.is_live_game:
                    _cancel_market_orders(live_mgr, slug, curr_orders)
                    ms.active = False
                    ms.deactivation_reason = "game_started"
                    discord_notify(
                        f"**LIVE MM** game started: {slug} "
                        f"inv={ms.net_inventory}")
                    continue

                # Layer 4 risk
                l4 = check_layer4(ms, spread, gs.db_error_count)
                if l4 not in (Action.CONTINUE, Action.SOFT_CLOSE):
                    if l4 == Action.PAUSE_60S:
                        ms.paused_until = now + timedelta(seconds=60)
                    elif l4 == Action.FULL_STOP:
                        for m in gs.markets.values():
                            m.active = False
                            m.deactivation_reason = "FULL_STOP (L4)"
                        live_mgr.cancel_all_orders()
                        discord_notify(
                            f"**LIVE MM FULL_STOP** L4 triggered by {slug}")
                    elif l4 == Action.EXIT_MARKET:
                        _cancel_market_orders(live_mgr, slug, curr_orders)
                        ms.active = False
                        ms.deactivation_reason = "EXIT_MARKET (L4)"
                    elif l4 == Action.CANCEL_ALL:
                        _cancel_market_orders(live_mgr, slug, curr_orders)
                    continue

                time_soft_close = (l4 == Action.SOFT_CLOSE)

                # Layer 2-3 risk
                actions = [Action.CONTINUE]
                l2 = check_layer2(ms, max_inventory=risk["max_inventory"])
                if l2 != Action.CONTINUE:
                    actions.append(l2)
                l3 = check_layer3(ms, gs)
                if l3 != Action.CONTINUE:
                    actions.append(l3)
                action = highest_priority(actions)

                if action == Action.FULL_STOP:
                    for m in gs.markets.values():
                        m.active = False
                        m.deactivation_reason = "FULL_STOP (L2/L3)"
                    live_mgr.cancel_all_orders()
                    discord_notify(
                        f"**LIVE MM FULL_STOP** L2/L3 by {slug}")
                    continue
                if action == Action.EXIT_MARKET:
                    _cancel_market_orders(live_mgr, slug, curr_orders)
                    ms.active = False
                    ms.deactivation_reason = "EXIT_MARKET (L2/L3)"
                    continue
                if action == Action.PAUSE_30MIN:
                    apply_pause_30min(ms)
                    _cancel_market_orders(live_mgr, slug, curr_orders)
                    continue
                if action in (Action.STOP_AND_FLATTEN,
                              Action.FORCE_CLOSE):
                    _cancel_market_orders(live_mgr, slug, curr_orders)
                    # Aggress flatten via aggressive maker order
                    _place_aggress_order(live_mgr, ms, best_yes_bid,
                                         yes_ask, best_no_bid, midpoint,
                                         args.size, risk)
                    continue
                if action == Action.AGGRESS_FLATTEN:
                    _place_aggress_order(live_mgr, ms, best_yes_bid,
                                         yes_ask, best_no_bid, midpoint,
                                         args.size, risk)

                # Manage quotes
                if action <= Action.AGGRESS_FLATTEN:
                    _manage_live_quotes(
                        live_mgr, ms, best_yes_bid, best_no_bid,
                        yes_ask, midpoint, yes_bids, no_bids,
                        curr_orders, args.size,
                        risk["max_inventory"],
                        time_soft_close=time_soft_close,
                        max_unhedged_exit=risk["max_unhedged_exit"],
                        inventory_changed=slug in inv_changed_slugs)

                # Snapshot every 6th tick
                tick_count += 1
                if tick_count % 6 == 0:
                    try:
                        db.insert_snapshot(
                            ts=now.isoformat(), ticker=slug,
                            best_yes_bid=best_yes_bid, yes_ask=yes_ask,
                            spread=spread, midpoint=midpoint,
                            net_inventory=ms.net_inventory,
                            yes_held=len(ms.yes_queue),
                            no_held=len(ms.no_queue),
                            realized_pnl=ms.realized_pnl,
                            unrealized_pnl=ms.unrealized_pnl,
                            total_pnl=ms.realized_pnl + ms.unrealized_pnl,
                            total_fees=ms.total_fees,
                            yes_order_price=None, yes_queue_pos=None,
                            no_order_price=None, no_queue_pos=None,
                            trade_volume_1min=0,
                            global_realized_pnl=gs.total_realized_pnl,
                            global_unrealized_pnl=gs.total_unrealized_pnl,
                            global_total_pnl=gs.total_pnl)
                    except Exception:
                        gs.db_error_count += 1

                # Terminal output
                ts = now.strftime("%H:%M:%S")
                short = slug[:16]
                print(f"  [{ts}] {short:16s} mid={midpoint:.0f}c "
                      f"sprd={spread} inv={ms.net_inventory} "
                      f"pnl={ms.realized_pnl:.1f}c", flush=True)

            cycle += 1

            # Periodic summary
            now_ts = time.time()
            if now_ts - last_summary_time >= SUMMARY_INTERVAL:
                elapsed_h = (now_ts - start) / 3600
                total_rebates = sum(rebates_earned.values())
                summary = (
                    f"**Poly LIVE MM 12h Summary** | {elapsed_h:.1f}h | "
                    f"{len(active_slugs)}/{len(slugs)} active | "
                    f"pnl={gs.total_pnl:.1f}c "
                    f"(+{total_rebates:.1f}c rebates) | "
                    f"session={session_id}")
                print(f"\n{'='*70}")
                print(f"12H SUMMARY ({elapsed_h:.1f}h)")
                for s, ms in gs.markets.items():
                    status = ("ACTIVE" if ms.active
                              else f"EXIT({ms.deactivation_reason})")
                    print(f"  {s}: inv={ms.net_inventory} "
                          f"pnl={ms.realized_pnl:.1f}c [{status}]")
                print(f"{'='*70}\n")
                discord_notify(summary)
                last_summary_time = now_ts

            time.sleep(sleep_time)

    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        discord_notify(f"**POLY LIVE MM FATAL**: {e} | session={session_id}")

    # Shutdown: cancel all orders
    print("\nCancelling all orders...", flush=True)
    live_mgr.cancel_all_orders()
    atexit.unregister(_crash_cleanup)

    # Session summary
    elapsed = time.time() - start
    total_rebates = sum(rebates_earned.values())
    gross_pnl = gs.total_pnl
    net_pnl = gross_pnl + total_rebates

    print(f"\n{'='*70}")
    print(f"SESSION SUMMARY — POLYMARKET US {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"{'='*70}")
    print(f"  Duration:           {elapsed/3600:.1f}h")
    print(f"  Session:            {session_id}")

    for slug, ms in gs.markets.items():
        rebate = rebates_earned.get(slug, 0)
        print(f"\n  {slug}:")
        print(f"    Realized P&L:     {ms.realized_pnl:.1f}c")
        print(f"    Unrealized P&L:   {ms.unrealized_pnl:.1f}c")
        print(f"    Maker rebates:    +{rebate:.1f}c")
        print(f"    Net inventory:    {ms.net_inventory}")
        if not ms.active:
            print(f"    Exit reason:      {ms.deactivation_reason}")

    print(f"\n  GLOBAL:")
    print(f"    Gross P&L:        {gross_pnl:.1f}c")
    print(f"    Maker rebates:    +{total_rebates:.1f}c")
    print(f"    Net P&L:          {net_pnl:.1f}c")
    print(f"    Peak P&L:         {gs.peak_total_pnl:.1f}c")
    print(f"    DB:               {args.db_path}")

    discord_notify(
        f"**Poly {'DRY-RUN' if args.dry_run else 'LIVE'} MM Ended** | "
        f"{elapsed/3600:.1f}h | gross={gross_pnl:.1f}c "
        f"rebates=+{total_rebates:.1f}c net={net_pnl:.1f}c | "
        f"session={session_id}")

    db.close()

    # Auto-generate session summary
    try:
        from scripts.session_summary import generate_summary
        summary = generate_summary(args.db_path, session_id)
        sessions_dir = Path(".claude/sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sessions_dir / f"poly-live-{session_id}.md"
        summary_path.write_text(summary)
        print(f"\nSession summary: {summary_path}")
    except Exception as e:
        print(f"  Warning: session summary failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Quote management helpers
# ---------------------------------------------------------------------------

def _cancel_market_orders(live_mgr: LiveOrderManager, slug: str,
                          curr_orders: dict):
    """Cancel all orders for a specific market."""
    slug_orders = curr_orders.get(slug, {})
    for side, info in slug_orders.items():
        live_mgr.cancel_order(slug, side, info["order_id"])


def _place_aggress_order(live_mgr: LiveOrderManager, ms: MarketState,
                         best_yes_bid: int, yes_ask: int,
                         best_no_bid: int, midpoint: float,
                         order_size: int, risk: dict):
    """Place aggressive maker order to flatten inventory."""
    net = ms.net_inventory
    if net == 0:
        return

    if net > 0:
        # Long YES → buy NO aggressively
        side = "no"
        price = clamp_price(soft_close_exit_price(
            side="no", fair_value=100 - midpoint,
            best_bid=best_no_bid, max_slippage=5))
        size = min(order_size, abs(net))
    else:
        # Long NO → buy YES aggressively
        side = "yes"
        price = clamp_price(soft_close_exit_price(
            side="yes", fair_value=midpoint,
            best_bid=best_yes_bid, max_slippage=5))
        size = min(order_size, abs(net))

    live_mgr.place_order(ms.ticker, side, price, size)
    print(f"    AGGRESS {ms.ticker}: {side}@{price}c x{size} "
          f"(inv={net})", flush=True)


def _manage_live_quotes(live_mgr: LiveOrderManager, ms: MarketState,
                        best_yes_bid: int, best_no_bid: int,
                        yes_ask: int, midpoint: float,
                        yes_bids: list, no_bids: list,
                        curr_orders: dict, order_size: int,
                        max_inventory: int,
                        time_soft_close: bool = False,
                        max_unhedged_exit: int = 5,
                        inventory_changed: bool = False):
    """Manage live quote placement with requote tolerance."""
    now = datetime.now(timezone.utc)
    net_inventory = ms.net_inventory
    slug = ms.ticker

    # Soft-close mode
    if ms.is_soft_close or time_soft_close:
        if net_inventory == 0:
            _cancel_market_orders(live_mgr, slug, curr_orders)
            return

        # Cancel side that increases inventory
        slug_orders = curr_orders.get(slug, {})
        if net_inventory > 0:
            if "yes" in slug_orders:
                live_mgr.cancel_order(slug, "yes",
                                      slug_orders["yes"]["order_id"])
            reduce_side = "no"
            reduce_bid = best_no_bid
        else:
            if "no" in slug_orders:
                live_mgr.cancel_order(slug, "no",
                                      slug_orders["no"]["order_id"])
            reduce_side = "yes"
            reduce_bid = best_yes_bid

        # Aggressive exit if inventory exceeds threshold
        if time_soft_close and abs(net_inventory) > max_unhedged_exit:
            price = soft_close_exit_price(
                side=reduce_side,
                fair_value=(midpoint if reduce_side == "yes"
                            else 100 - midpoint),
                best_bid=reduce_bid, max_slippage=5)
            size = min(order_size, abs(net_inventory))

            existing = slug_orders.get(reduce_side)
            if existing is None or should_requote_or_force(
                    price, existing["price_cents"], force_requote=True):
                if existing:
                    live_mgr.cancel_order(slug, reduce_side,
                                          existing["order_id"])
                live_mgr.place_order(slug, reduce_side, price, size)
                print(f"    SOFT-EXIT {slug}: {reduce_side}@{price}c "
                      f"(inv={net_inventory})", flush=True)
        return

    # Dynamic spread
    market_spread = yes_ask - best_yes_bid
    vol_offset = dynamic_spread(ms.midpoint_history, now) - market_spread
    vol_offset = max(0, vol_offset)

    # Skewed quotes
    yes_quote, no_quote = skewed_quotes(
        fair=midpoint, best_yes_bid=best_yes_bid,
        best_no_bid=best_no_bid,
        net_inventory=net_inventory, gamma=0.5,
        quote_offset=vol_offset)

    slug_orders = curr_orders.get(slug, {})

    for side, quote_price, best_bid, bids in [
            ("yes", yes_quote, best_yes_bid, yes_bids),
            ("no", no_quote, best_no_bid, no_bids)]:

        # Cooldown check
        if is_side_cooled_down(ms, side, now):
            existing = slug_orders.get(side)
            if existing:
                live_mgr.cancel_order(slug, side, existing["order_id"])
            continue

        # Inventory cap
        if should_skip_side(side, net_inventory, max_inventory):
            existing = slug_orders.get(side)
            if existing:
                live_mgr.cancel_order(slug, side, existing["order_id"])
            continue

        # Clamp size
        size = clamp_order_size(side, net_inventory, order_size,
                                max_inventory)
        if size <= 0:
            continue

        # Layer 1 validation
        rejection = check_layer1(quote_price, size, midpoint, side=side)
        if rejection:
            continue

        existing = slug_orders.get(side)

        if existing is not None:
            force = inventory_changed
            if not should_requote_or_force(
                    quote_price, existing["price_cents"],
                    force_requote=force):
                delta = abs(quote_price - existing["price_cents"])
                print(f"    SKIP_REQUOTE {slug} {side} "
                      f"old={existing['price_cents']}c new={quote_price}c "
                      f"delta={delta}c", flush=True)
                continue  # keep existing order — preserve queue priority
            # Cancel old order
            live_mgr.cancel_order(slug, side, existing["order_id"])

        # Place new order
        live_mgr.place_order(slug, side, quote_price, size)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user.", flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            discord_notify(f"**POLY LIVE MM CRASHED**: {e}")
        except Exception:
            pass
        sys.exit(1)
