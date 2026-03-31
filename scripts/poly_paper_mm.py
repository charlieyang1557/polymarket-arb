#!/usr/bin/env python3
"""
Paper trading market maker for Polymarket US.

Reuses the Kalshi MM engine (OBI microprice, continuous skew, dynamic spread,
4-layer risk) but with PolyClient adapter. Paper-only: no real orders placed.

Key differences from Kalshi paper_mm.py:
  - Uses PolyClient instead of KalshiClient
  - Slugs instead of tickers
  - Maker rebates (negative fees) instead of maker fees
  - No trades endpoint — fill simulation via orderbook snapshots
  - Separate DB: data/poly_mm_paper.db

Usage:
    python scripts/poly_paper_mm.py --slugs SLUG1,SLUG2 --duration 300
    python scripts/poly_paper_mm.py --slugs SLUG1 --duration 86400 --size 2
"""

import argparse
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.poly_client import PolyClient, calculate_maker_fee
import src.mm.state as _mm_state
from src.mm.state import MarketState, GlobalState, SimOrder
from src.mm.engine import MMEngine, discord_notify, process_fills
from src.mm.db import MMDatabase


def _apply_poly_fee_patch():
    """Replace Kalshi maker fee with Polymarket rebate.

    Kalshi charges makers (positive fee). Polymarket PAYS makers (negative fee).
    This affects _record_fill() and any other fee calculation in the engine.
    Must be called before engine starts processing fills.
    """
    _mm_state.maker_fee_cents = lambda price_cents, count=1: calculate_maker_fee(
        price_cents, category="sports", count=count)


# ---------------------------------------------------------------------------
# Orderbook-snapshot fill simulation (tested)
# ---------------------------------------------------------------------------

WORST_CASE_PER_CONTRACT = 50  # cents at midpoint 50c
MAX_SKEW_CENTS = 5  # max skew at full inventory


def compute_risk_params(capital_cents: int) -> dict:
    """Derive risk thresholds from capital.

    $25 (2500c): MAX_INV=10, SIZE=2, AGGRESS=8, gamma=0.5
    $200 (20000c): MAX_INV=80, SIZE=16, AGGRESS=64, gamma=0.0625
    """
    max_inv = max(4, int(capital_cents * 0.20 / WORST_CASE_PER_CONTRACT))
    max_unhedged = max(2, int(capital_cents * 0.10 / WORST_CASE_PER_CONTRACT))
    aggress_thresh = max(2, int(max_inv * 0.8))
    order_size = max(1, int(max_inv / 5))
    gamma = MAX_SKEW_CENTS / max_inv  # normalized: skew = (inv/MAX_INV) * MAX_SKEW
    return {
        "max_inventory": max_inv,
        "max_unhedged_exit": max_unhedged,
        "aggress_threshold": aggress_thresh,
        "order_size": order_size,
        "gamma": gamma,
    }


def should_soft_close_flatten(net_inventory: int,
                                max_unhedged_exit: int) -> bool:
    """Whether to flatten during SOFT_CLOSE. Only if |inv| > threshold."""
    return abs(net_inventory) > max_unhedged_exit


DRAIN_FACTOR = 0.5  # conservative: only 50% of depth decrease = real trades
MAX_DRAIN_PER_TICK = 50000  # $500 worth — cap extreme depth swings
MAX_ACTIVE_MARKETS = 10
ACTIVE_SLUGS_PATH = "data/poly_active_slugs.json"
PENDING_MARKETS_PATH = "data/pending_poly_markets.json"


def compute_depth_at_price(book: list[list], price: int,
                            side: str = "yes") -> int:
    """Total depth at or below our price level.

    book: [[price_cents, qty], ...] sorted ascending.
    For both YES and NO sides, depth at price P = sum of qty where level <= P.
    These are the contracts ahead of us in the FIFO queue.
    """
    total = 0
    for p, q in book:
        if p <= price:
            total += q
    return total


def compute_drain(prev_depth: int, curr_depth: int,
                   factor: float = DRAIN_FACTOR) -> int:
    """Drain from depth decrease, capped at MAX_DRAIN_PER_TICK."""
    delta = prev_depth - curr_depth
    if delta <= 0:
        return 0
    raw = int(delta * factor)
    return min(raw, MAX_DRAIN_PER_TICK)


class DepthFillSimulator:
    """Simulates fills via orderbook depth changes.

    Tracks depth at our resting price levels across ticks.
    When depth decreases, advances queue position by drain * factor.
    When queue reaches 0, triggers fill.
    """

    def __init__(self, factor: float = DRAIN_FACTOR):
        self.factor = factor
        # Track prev state per slug: {slug: {"yes": (price, depth), "no": ...}}
        self._prev: dict[str, dict] = {}

    def check_fills(self, slug: str,
                     yes_order: SimOrder | None,
                     no_order: SimOrder | None,
                     yes_book: list[list],
                     no_book: list[list]) -> list[dict]:
        """Check for fills based on depth changes.

        Returns list of fill dicts: [{"side": ..., "filled": ..., "price": ...}]
        """
        fills = []
        prev = self._prev.get(slug, {})
        new_prev: dict = {}

        for order, book, side_key in [
            (yes_order, yes_book, "yes"),
            (no_order, no_book, "no"),
        ]:
            if order is None or order.remaining <= 0:
                new_prev[side_key] = None
                continue

            curr_depth = compute_depth_at_price(book, order.price, side_key)
            new_prev[side_key] = (order.price, curr_depth)

            # Check if we have a baseline at this price
            prev_entry = prev.get(side_key)
            if prev_entry is None:
                continue  # first tick — set baseline only

            prev_price, prev_depth = prev_entry
            if prev_price != order.price:
                continue  # order replaced — reset baseline

            raw_drain = int((prev_depth - curr_depth) * self.factor)
            drain = compute_drain(prev_depth, curr_depth, self.factor)
            if raw_drain > MAX_DRAIN_PER_TICK:
                print(f"    DRAIN_CAP: {slug} {side_key}@{order.price} "
                      f"raw_drain={raw_drain} capped to {drain}",
                      flush=True)
            if drain > 0:
                filled = process_fills(order, drain)
                if filled > 0:
                    fills.append({
                        "side": side_key,
                        "filled": filled,
                        "price": order.price,
                        "drain": drain,
                        "prev_depth": prev_depth,
                        "curr_depth": curr_depth,
                    })

        self._prev[slug] = new_prev
        return fills


def write_active_slugs_file(slugs: list[str], session_id: str,
                             path: str = ACTIVE_SLUGS_PATH):
    """Write current active slugs to state file (atomic)."""
    data = {
        "session_id": session_id,
        "active_slugs": slugs,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, path)


def consume_pending_markets(gs, pending_path: str = PENDING_MARKETS_PATH,
                             active_path: str = ACTIVE_SLUGS_PATH,
                             game_start_lookup=None) -> list[str]:
    """Consume pending hot-add file and add markets to GlobalState.

    Returns list of slug strings that were actually added.
    """
    if not os.path.exists(pending_path):
        return []

    # Atomic consume: rename → read → delete
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
        # Skip duplicates
        if slug in gs.markets:
            print(f"  SKIP duplicate hot-add: {slug}", flush=True)
            continue

        # Max cap
        if active_count >= MAX_ACTIVE_MARKETS:
            print(f"  MAX_CAP: cannot add {slug} "
                  f"({active_count}/{MAX_ACTIVE_MARKETS})", flush=True)
            break

        # Get game start time
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

        gst_note = f" (game {game_start.strftime('%H:%M')}Z)" if game_start else ""
        print(f"  HOT-ADD: {slug}{gst_note}", flush=True)

    # Update active slugs file
    if added:
        active_slugs = [s for s, ms in gs.markets.items() if ms.active]
        write_active_slugs_file(active_slugs, gs.session_id, active_path)

    return added


def main():
    # Replace Kalshi fee formula with Polymarket rebate BEFORE engine init
    _apply_poly_fee_patch()

    parser = argparse.ArgumentParser(
        description="Paper trading market maker — Polymarket US")
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
    parser.add_argument("--db-path", default="data/poly_mm_paper.db")
    args = parser.parse_args()

    # Auth is optional for paper trading (we only read orderbooks)
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret_key = os.getenv("POLYMARKET_SECRET_KEY")

    if key_id and secret_key:
        client = PolyClient(key_id=key_id, secret_key=secret_key)
        print("  Auth: configured (read + write)")
    else:
        client = PolyClient()
        print("  Auth: public only (read-only)")

    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + \
                 uuid.uuid4().hex[:6]

    db = MMDatabase(args.db_path, session_id)
    gs = GlobalState(session_id=session_id)

    # Load game start times from scanner targets + SDK
    schedule = {}

    # Source 1: Scanner daily_targets.json (has game_start_time from SDK/schedule)
    targets_file = Path("data/polymarket_diagnostic/daily_targets.json")
    try:
        with open(targets_file) as f:
            for t in json.load(f):
                gst = t.get("game_start_time") or ""
                if gst and t.get("slug"):
                    schedule[t["slug"]] = gst
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Source 2: Direct SDK lookup for any slugs not in targets
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

    matched = sum(1 for s in slugs if s in schedule)
    print(f"  Game start times: {matched}/{len(slugs)} slugs matched")

    # Initialize markets with game_start_utc for time-based exit
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

    risk = compute_risk_params(args.capital)
    order_size = risk["order_size"] if args.size == 2 else args.size  # CLI overrides auto
    engine = MMEngine(client, db, gs, order_size=order_size,
                      max_inventory=risk["max_inventory"],
                      max_unhedged_exit=risk["max_unhedged_exit"],
                      gamma=risk["gamma"])
    fill_sim = DepthFillSimulator(factor=DRAIN_FACTOR)

    # Track rebates earned per market for session summary
    rebates_earned = {slug: 0.0 for slug in slugs}

    # Graceful shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        shutdown = True
        print("\nShutting down gracefully...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Header
    n = len(slugs)
    MM_VERSION = "v3: Polymarket US — OBI + normalized skew + capital scaling"
    print(f"\nPoly Paper MM | {MM_VERSION}")
    print(f"  {n} markets | {order_size} contracts | "
          f"{args.interval}s interval")
    print(f"  Capital: ${args.capital/100:.0f} | "
          f"MAX_INV: {risk['max_inventory']} | SIZE: {order_size} | "
          f"UNHEDGED: {risk['max_unhedged_exit']} | "
          f"AGGRESS: {risk['aggress_threshold']} | "
          f"GAMMA: {risk['gamma']:.4f}")
    print(f"  Session: {session_id}")
    print(f"  Started: {datetime.now(timezone.utc).isoformat()} | "
          f"Duration: {args.duration}s | DB: {args.db_path}")
    print("-" * 70)

    discord_notify(
        f"**Poly Paper MM Started** | {n} markets | session={session_id}\n"
        f"Slugs: {', '.join(slugs)}")

    # Write initial active slugs state
    write_active_slugs_file(slugs, session_id)

    active_slugs = list(slugs)
    start = time.time()
    cycle = 0
    last_summary_time = start
    SUMMARY_INTERVAL = 43200  # 12h

    def _lookup_game_start(slug):
        """Look up gameStartTime from SDK for hot-added slugs."""
        try:
            raw = client.get_market(slug)
            market = raw.get("market", raw) or {}
            return market.get("gameStartTime") or ""
        except Exception:
            return ""

    try:
        while not shutdown and (time.time() - start) < args.duration:
            # --- Hot-add: check for pending markets from scanner ---
            added = consume_pending_markets(
                gs, game_start_lookup=_lookup_game_start)
            if added:
                for slug in added:
                    rebates_earned[slug] = 0.0
                discord_notify(
                    f"**Hot-added {len(added)} markets** | "
                    f"session={session_id}\n" +
                    ", ".join(added))

            # Refresh active slugs from all markets in gs
            all_slugs = list(gs.markets.keys())
            active_slugs = [s for s in all_slugs if gs.markets[s].active]
            if not active_slugs:
                print("All markets inactive. Stopping.")
                break

            sleep_time = args.interval / max(len(active_slugs), 1)

            for i, slug in enumerate(active_slugs):
                if shutdown:
                    break
                if cycle % len(active_slugs) != i:
                    continue

                ms = gs.markets[slug]
                try:
                    engine.tick_one_market(ms)

                    # --- Depth-based fill simulation ---
                    # Engine's trade-based drain was a no-op (empty trades).
                    # Now check for fills via orderbook depth changes.
                    if ms.active and (ms.yes_order or ms.no_order):
                        try:
                            book_data = client.get_orderbook(slug)
                            fp = book_data.get("orderbook_fp", {})
                            yes_raw = fp.get("yes_dollars", [])
                            no_raw = fp.get("no_dollars", [])
                            yes_book = [[round(float(p) * 100), int(float(q))]
                                        for p, q in yes_raw]
                            no_book = [[round(float(p) * 100), int(float(q))]
                                       for p, q in no_raw]

                            fills = fill_sim.check_fills(
                                slug, ms.yes_order, ms.no_order,
                                yes_book, no_book)

                            for f in fills:
                                order = (ms.yes_order if f["side"] == "yes"
                                         else ms.no_order)
                                if order is None:
                                    continue

                                print(f"    DEPTH {slug} "
                                      f"{f['side']}@{f['price']}: "
                                      f"{f['prev_depth']}→{f['curr_depth']} "
                                      f"drain={f['drain']} "
                                      f"qpos→{order.queue_pos}",
                                      flush=True)

                                # Record fill via engine's pipeline
                                best_yb = yes_book[-1][0] if yes_book else 50
                                best_nb = no_book[-1][0] if no_book else 50
                                engine._record_fill(
                                    ms, order, f["filled"],
                                    best_yb, best_nb)

                                # Track rebate (negative fee = income)
                                rebate = abs(calculate_maker_fee(
                                    f["price"], count=f["filled"]))
                                rebates_earned[slug] += rebate

                        except Exception as e:
                            pass  # non-critical: fill sim failure is OK

                except Exception as e:
                    print(f"  !!! API ERROR {slug}: {e}",
                          file=sys.stderr, flush=True)
                    try:
                        engine._cancel_orders(ms, f"unexpected_error: {e}")
                    except Exception:
                        pass

            cycle += 1

            # Update active slugs file if market set changed
            curr_active = [s for s in all_slugs if gs.markets[s].active]
            if set(curr_active) != set(active_slugs):
                write_active_slugs_file(curr_active, session_id)

            # Periodic summary
            now_ts = time.time()
            if now_ts - last_summary_time >= SUMMARY_INTERVAL:
                elapsed_h = (now_ts - start) / 3600
                active_count = len(active_slugs)
                total_rebates = sum(rebates_earned.values())
                summary = (
                    f"**Poly Paper MM 12h Summary** | {elapsed_h:.1f}h | "
                    f"{active_count}/{n} active | "
                    f"pnl={gs.total_pnl:.1f}c "
                    f"(+{total_rebates:.1f}c rebates) | "
                    f"session={session_id}")
                print(f"\n{'=' * 70}")
                print(f"12H SUMMARY ({elapsed_h:.1f}h)")
                print(f"  Active: {active_count}/{n} markets")
                print(f"  Total P&L: {gs.total_pnl:.1f}c | "
                      f"Rebates: +{total_rebates:.1f}c")
                for s, ms in gs.markets.items():
                    status = ("ACTIVE" if ms.active
                              else f"EXIT({ms.deactivation_reason})")
                    print(f"  {s}: inv={ms.net_inventory} "
                          f"pnl={ms.realized_pnl:.1f}c [{status}]")
                print(f"{'=' * 70}\n")
                discord_notify(summary)
                last_summary_time = now_ts

            time.sleep(sleep_time)

    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        discord_notify(f"**POLY MM FATAL**: {e} | session={session_id}")

    # Shutdown: cancel orders and write final snapshots
    for ms in gs.markets.values():
        engine._cancel_orders(ms, "shutdown")
        if ms.midpoint_history:
            mid = ms.midpoint_history[-1][1]
            best_yb = int(mid - 2)
            y_ask = int(mid + 2)
            engine._write_snapshot(ms, best_yb, y_ask,
                                   y_ask - best_yb, mid)

    # Session summary
    elapsed = time.time() - start
    total_rebates = sum(rebates_earned.values())
    gross_pnl = gs.total_pnl
    net_pnl = gross_pnl + total_rebates

    print(f"\n{'=' * 70}")
    print("SESSION SUMMARY — POLYMARKET US")
    print(f"{'=' * 70}")
    print(f"  Duration:           {elapsed/3600:.1f}h")
    print(f"  Session:            {session_id}")
    print(f"  Platform:           Polymarket US (maker rebates)")

    for slug, ms in gs.markets.items():
        rebate = rebates_earned.get(slug, 0)
        print(f"\n  {slug}:")
        print(f"    Realized P&L:     {ms.realized_pnl:.1f}c")
        print(f"    Unrealized P&L:   {ms.unrealized_pnl:.1f}c")
        print(f"    Maker rebates:    +{rebate:.1f}c")
        print(f"    Net inventory:    {ms.net_inventory}")
        print(f"    Active:           {ms.active}")
        if not ms.active:
            print(f"    Exit reason:      {ms.deactivation_reason}")

    print(f"\n  GLOBAL:")
    print(f"    Gross P&L:        {gross_pnl:.1f}c")
    print(f"    Maker rebates:    +{total_rebates:.1f}c")
    print(f"    Net P&L:          {net_pnl:.1f}c")
    print(f"    Peak P&L:         {gs.peak_total_pnl:.1f}c")
    print(f"    DB:               {args.db_path}")

    discord_notify(
        f"**Poly Paper MM Ended** | {elapsed/3600:.1f}h | "
        f"gross={gross_pnl:.1f}c rebates=+{total_rebates:.1f}c "
        f"net={net_pnl:.1f}c | session={session_id}")

    db.close()

    # Auto-generate session summary
    try:
        from scripts.session_summary import generate_summary
        summary = generate_summary(args.db_path, session_id)
        sessions_dir = Path(".claude/sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sessions_dir / f"poly-{session_id}.md"
        summary_path.write_text(summary)
        print(f"\nSession summary: {summary_path}")
    except Exception as e:
        print(f"  Warning: session summary failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user.", flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            from src.mm.engine import discord_notify
            discord_notify(f"**POLY MM CRASHED**: {e}")
        except Exception:
            pass
        sys.exit(1)
