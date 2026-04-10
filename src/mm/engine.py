# src/mm/engine.py
"""Core engine for the paper market maker."""

from __future__ import annotations
import json
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from src.mm.state import (
    SimOrder, MarketState, GlobalState,
    maker_fee_cents, taker_fee_cents, unrealized_pnl_cents,
    obi_microprice, skewed_quotes, dynamic_spread,
    ExitLadderStep, DEFAULT_EXIT_LADDER, TAKER_CROSS_SECONDS,
)
from src.mm.risk import Action, check_layer1, check_layer2, check_layer3, check_layer4, highest_priority, apply_pause_30min
from src.mm.db import MMDatabase
from src.kalshi_client import KalshiClient

import requests as _requests  # for discord

logger = logging.getLogger(__name__)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
MAX_INVENTORY = 10  # single-side cap
PENDING_MARKETS_PATH = "data/pending_markets.json"
MAX_ACTIVE_MARKETS = 15


def load_pending_markets(gs: GlobalState, path: str = PENDING_MARKETS_PATH,
                         max_active: int = MAX_ACTIVE_MARKETS) -> list[str]:
    """Check for pending_markets.json, add new markets to session.

    Atomic consume: rename to .processing first, then read and process,
    then delete. Prevents race condition where scanner writes a new file
    while engine is mid-processing.

    Returns list of ticker strings that were added.
    """
    if not os.path.exists(path):
        return []

    # Atomic consume: claim the file before reading
    processing_path = path + ".processing"
    try:
        os.rename(path, processing_path)
    except OSError:
        return []  # another process claimed it, or disappeared

    try:
        with open(processing_path) as f:
            pending = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Malformed pending_markets.json: %s", e)
        os.remove(processing_path)
        return []

    active_count = sum(1 for m in gs.markets.values() if m.active)
    added = []

    for entry in pending:
        ticker = entry.get("ticker")
        if not ticker or ticker in gs.markets:
            continue
        if active_count >= max_active:
            break

        # Parse game_start_utc if present
        game_start = None
        raw_start = entry.get("game_start_utc")
        if raw_start:
            try:
                game_start = datetime.fromisoformat(
                    raw_start.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        gs.markets[ticker] = MarketState(
            ticker=ticker, game_start_utc=game_start)
        added.append(ticker)
        active_count += 1

    os.remove(processing_path)
    return added


def clamp_order_size(side: str, net_inventory: int, order_size: int,
                     max_inventory: int = MAX_INVENTORY) -> int:
    """Clamp order size based on inventory position.

    Making side (increases |inv|): reduced proportionally.
      making_size = max(1, order_size - |inv|)
      Returns 0 at max_inventory (should_skip_side handles this).

    Reducing side (decreases |inv|): keeps full size.
    """
    abs_inv = abs(net_inventory)
    if side == "yes" and net_inventory >= 0:
        hard_cap = max(0, max_inventory - net_inventory)
        soft_cap = max(1, order_size - abs_inv) if hard_cap > 0 else 0
        return min(soft_cap, hard_cap)
    if side == "no" and net_inventory <= 0:
        hard_cap = max(0, max_inventory + net_inventory)
        soft_cap = max(1, order_size - abs_inv) if hard_cap > 0 else 0
        return min(soft_cap, hard_cap)
    # Reducing side — no clamp needed
    return order_size


def progressive_exit_price(side: str, fair_value: float, best_bid: int,
                           best_ask: int, seconds_to_game: float,
                           max_slippage: int = 5,
                           max_taker_loss: int = 10,
                           ladder: tuple[ExitLadderStep, ...] = DEFAULT_EXIT_LADDER,
                           ) -> int | None:
    """Time-decayed exit pricing for SOFT_CLOSE window.

    Walks the ladder from longest to shortest time horizon.
    Below TAKER_CROSS_SECONDS, attempts to cross the spread.

    Returns:
      int: price in cents to place the exit order
      None: book is empty or spread too wide — accept settlement risk
    """
    fair_int = int(fair_value)
    cap = fair_int + max_slippage

    # Taker cross: limit order at ask+1 executes as taker on Polymarket.
    # Taker fee (~0.5c at mid=50) is on top of max_taker_loss budget.
    if seconds_to_game < TAKER_CROSS_SECONDS:
        if best_ask <= 0:
            return None  # empty book
        ask_cost = best_ask - fair_int
        if ask_cost > max_taker_loss:
            return None  # book too wide — accept settlement
        price = best_ask + 1
        return max(1, min(99, min(price, cap)))

    price = fair_int
    for step in ladder:
        if seconds_to_game <= step.seconds_threshold:
            price = fair_int + step.price_offset
    price = min(price, cap)
    return max(1, min(99, price))


def soft_close_exit_price(side: str, fair_value: float, best_bid: int,
                          max_slippage: int = 5) -> int:
    """Legacy wrapper — used by AGGRESS_FLATTEN (no time component)."""
    aggressive = best_bid + 1
    cap = int(fair_value) + max_slippage
    return max(1, min(aggressive, cap))


def is_side_cooled_down(ms: MarketState, side: str,
                         now: datetime) -> bool:
    """Check if a side is in post-AGGRESS_FLATTEN cooldown."""
    cd = (ms.aggress_cooldown_yes if side == "yes"
          else ms.aggress_cooldown_no)
    if cd is None:
        return False
    return now < cd


def should_skip_side(side: str, net_inventory: int,
                     max_inventory: int = MAX_INVENTORY) -> bool:
    """Skip quoting on the side that would increase inventory past cap."""
    if side == "yes" and net_inventory >= max_inventory:
        return True
    if side == "no" and net_inventory <= -max_inventory:
        return True
    return False


def should_disable_quoting(total_fills: int, paired_fills: int,
                           session_age_s: float,
                           min_fills: int = 3,
                           min_session_age_s: float = 7200,
                           min_paired_rate: float = 0.20) -> bool:
    """Check if quoting should be disabled due to low round-trip fill rate.

    Conditions (all must be true):
      - total_fills >= min_fills (avoid small sample bias)
      - session_age >= min_session_age_s (give market time to develop)
      - paired_rate < min_paired_rate (paired_fills*2 / total_fills)

    paired_fills counts round-trips (each = 2 individual fills paired off).
    """
    if total_fills < min_fills:
        return False
    if session_age_s < min_session_age_s:
        return False
    paired_rate = (paired_fills * 2) / total_fills
    return paired_rate < min_paired_rate


# -- Fill simulation helpers -----------------------------------------------

def drain_queue(order: SimOrder, trades: list[dict]) -> int:
    """Count trade volume that drains queue ahead of our order.

    YES bid at P: drain from trades where yes_price_cents <= P
    NO bid at P:  drain from trades where (100 - yes_price_cents) <= P
    """
    total = 0
    for t in trades:
        count = float(t.get("count_fp", 0) or 0)
        yes_price_cents = round(
            float(t.get("yes_price_dollars", 0) or 0) * 100)

        if order.side == "yes":
            if yes_price_cents <= order.price:
                total += count
        else:  # "no"
            no_price_cents = 100 - yes_price_cents
            if no_price_cents <= order.price:
                total += count
    return int(total)


def process_fills(order: SimOrder, drain: int) -> int:
    """Apply drain to order queue position. Returns number filled."""
    if order.queue_pos > 0:
        if drain <= order.queue_pos:
            order.queue_pos -= drain
            return 0
        overflow = drain - order.queue_pos
        order.queue_pos = 0
        filled = min(order.remaining, overflow)
    else:
        filled = min(order.remaining, drain)

    order.remaining -= filled
    return int(filled)


def pair_off_inventory(ms: MarketState) -> list[dict]:
    """Settle matched YES+NO pairs. Returns list of pair results."""
    pairs = []
    while ms.yes_queue and ms.no_queue:
        yes_cost = ms.yes_queue.pop(0)
        no_cost = ms.no_queue.pop(0)
        gross = 100 - yes_cost - no_cost
        pairs.append({
            "yes_cost": yes_cost, "no_cost": no_cost,
            "gross_pnl": gross,
        })
    return pairs


# -- Discord ---------------------------------------------------------------

def discord_notify(msg: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        _requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
    except Exception:
        pass


# -- Engine ----------------------------------------------------------------

class MMEngine:
    """Runs the paper market making simulation."""

    def __init__(self, client: KalshiClient, db: MMDatabase,
                 global_state: GlobalState, order_size: int = 2,
                 max_inventory: int = MAX_INVENTORY,
                 max_unhedged_exit: int = 5):
        self.client = client
        self.db = db
        self.gs = global_state
        self.order_size = order_size
        self.max_inventory = max_inventory
        self.max_unhedged_exit = max_unhedged_exit
        self.tick_count = 0  # per-market tick counter (for snapshot every 6th)

    def tick_one_market(self, ms: MarketState):
        """Execute one tick cycle for a single market."""
        now = datetime.now(timezone.utc)

        # Check pause
        if ms.paused_until and now < ms.paused_until:
            return

        # -- 1. Fetch book + trades --
        # Use min_ts to only fetch trades from last 5 minutes
        min_ts = int(now.timestamp()) - 300
        try:
            book_data = self.client.get_orderbook(ms.ticker, depth=20)
            trade_data = self.client.get_trades(ms.ticker, limit=100,
                                                 min_ts=min_ts)
            ms.last_api_success = now
        except _requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code in (401, 403, 404):
                reason = f"fatal HTTP {code}"
                self._log_event(ms, 4, Action.EXIT_MARKET, reason)
                ms.active = False
                ms.deactivation_reason = reason
                return
            self._log_event(ms, 4, Action.SKIP_TICK,
                            f"transient HTTP {code}")
            return
        except Exception as e:
            self._log_event(ms, 4, Action.SKIP_TICK, f"error: {e}")
            return

        # Parse book — API returns orderbook_fp with dollar strings
        book_fp = book_data.get("orderbook_fp", {})
        yes_bids_raw = book_fp.get("yes_dollars", [])
        no_bids_raw = book_fp.get("no_dollars", [])
        if not yes_bids_raw or not no_bids_raw:
            # Fallback to legacy format
            book = book_data.get("orderbook", book_data)
            yes_bids_raw = book.get("yes", [])
            no_bids_raw = book.get("no", [])
        if not yes_bids_raw or not no_bids_raw:
            ms.consecutive_skip_ticks += 1
            if ms.consecutive_skip_ticks >= 30:
                self._cancel_orders(ms, "orderbook_dead")
                ms.active = False
                ms.deactivation_reason = "orderbook_dead"
                reason = (f"orderbook dead ({ms.consecutive_skip_ticks} "
                          f"consecutive empty ticks) — deactivating {ms.ticker}")
                print(f"  >>> {reason}")
                discord_notify(f"**Paper MM** {reason}")
                self._log_event(ms, 4, Action.EXIT_MARKET, reason)
            elif ms.consecutive_skip_ticks == 1:
                # Log only on first skip, not every tick
                empty_side = ("yes" if not yes_bids_raw else "") + \
                             (" no" if not no_bids_raw else "")
                self._log_event(ms, 4, Action.SKIP_TICK,
                                f"empty orderbook side(s):{empty_side.strip()}")
            return

        # Good book received — reset skip counter
        ms.consecutive_skip_ticks = 0

        # Convert to [price_cents, quantity] integer pairs
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

        # Set session initial midpoint on first tick (for drift detection)
        if ms.session_initial_midpoint is None:
            ms.session_initial_midpoint = midpoint

        # Update midpoint history (keep last 7 entries ~70s)
        ms.midpoint_history.append((now, midpoint))
        if len(ms.midpoint_history) > 7:
            ms.midpoint_history.pop(0)

        # -- Pre-game only: exit when live game detected --
        if ms.is_live_game:
            self._cancel_orders(ms, "game_started")
            reason = (f"GAME STARTED — exiting {ms.ticker} "
                      f"inv={ms.net_inventory} pnl={ms.realized_pnl:.1f}c")
            ms.active = False
            ms.deactivation_reason = reason
            print(f"  >>> {reason}")
            discord_notify(f"**Paper MM** {reason}")
            self._log_event(ms, 4, Action.EXIT_MARKET, reason)
            return

        # -- 2. Layer 4 system checks --
        l4 = check_layer4(ms, spread, self.gs.db_error_count)
        if l4 not in (Action.CONTINUE, Action.SOFT_CLOSE):
            self._log_event(ms, 4, l4,
                            f"spread={spread} mid={midpoint:.1f}")
            if l4 == Action.PAUSE_60S:
                ms.paused_until = now + timedelta(seconds=60)
            elif l4 == Action.FULL_STOP:
                self._cancel_orders(ms, "risk_l4")
                reason = f"FULL_STOP triggered by {ms.ticker} (L4)"
                for m in self.gs.markets.values():
                    m.active = False
                    m.deactivation_reason = reason
                    if m.ticker != ms.ticker:
                        self._log_event(m, 4, Action.FULL_STOP,
                                        f"collateral shutdown: {reason}")
            elif l4 == Action.EXIT_MARKET:
                self._cancel_orders(ms, "risk_l4")
                ms.active = False
                ms.deactivation_reason = f"EXIT_MARKET (L4)"
                if ms.game_start_utc:
                    inv = ms.net_inventory
                    inv_note = ""
                    if inv != 0:
                        inv_note = (f" UNHEDGED EXIT: inv={inv}, "
                                    f"accepting residual risk")
                    reason = (f"TIME-BASED EXIT: game started — "
                              f"exiting {ms.ticker} inv={inv} "
                              f"pnl={ms.realized_pnl:.1f}c{inv_note}")
                    print(f"  >>> {reason}", flush=True)
                    discord_notify(f"**Paper MM** {reason}")
            elif l4 == Action.CANCEL_ALL:
                # Cancel orders but DON'T deactivate — market resumes next tick
                self._cancel_orders(ms, "risk_l4")
            return

        # Time-based soft-close: reduce-only mode before game start
        time_soft_close = (l4 == Action.SOFT_CLOSE)
        if time_soft_close and not getattr(ms, '_time_soft_close_logged', False):
            seconds_left = (ms.game_start_utc - now).total_seconds()
            reason = (f"TIME-BASED SOFT-CLOSE: game starts in "
                      f"{seconds_left / 60:.0f}min")
            print(f"  >>> {reason}")
            self._log_event(ms, 4, l4,
                            f"spread={spread} mid={midpoint:.1f} {reason}")
            discord_notify(f"**Paper MM** {ms.ticker}: {reason}")
            ms._time_soft_close_logged = True

        # -- 3. Filter new trades & drain queues --
        all_trades = trade_data.get("trades", [])

        # On first tick, just set the watermark — don't process historical trades
        if not ms.last_seen_trade_ts:
            if all_trades:
                ms.last_seen_trade_ts = max(
                    t.get("created_time", "") for t in all_trades)
                ms.last_seen_trade_ids = {
                    t["trade_id"] for t in all_trades
                    if t.get("created_time", "") == ms.last_seen_trade_ts
                }
            else:
                ms.last_seen_trade_ts = now.strftime(
                    "%Y-%m-%dT%H:%M:%S.000000Z")
            new_trades = []
        else:
            # Include trades strictly newer than watermark,
            # PLUS trades at the watermark timestamp with unseen trade_ids
            wm = ms.last_seen_trade_ts
            new_trades = [
                t for t in all_trades
                if t.get("created_time", "") > wm
                or (t.get("created_time", "") == wm
                    and t.get("trade_id") not in ms.last_seen_trade_ids)
            ]
            if new_trades:
                new_max = max(t.get("created_time", "") for t in new_trades)
                if new_max > wm:
                    # Advance watermark
                    ms.last_seen_trade_ts = new_max
                    ms.last_seen_trade_ids = {
                        t["trade_id"] for t in new_trades
                        if t.get("created_time", "") == new_max
                    }
                else:
                    # Same timestamp — just add the new trade_ids
                    ms.last_seen_trade_ids.update(
                        t["trade_id"] for t in new_trades
                    )

        # Populate trade_timestamps for live-game detection
        for t in new_trades:
            ct = t.get("created_time", "")
            if ct:
                try:
                    ts = datetime.fromisoformat(
                        ct.replace("Z", "+00:00"))
                    ms.trade_timestamps.append(ts)
                except (ValueError, TypeError):
                    pass
        # Prune old timestamps (keep last 10 min)
        cutoff = now - timedelta(minutes=10)
        ms.trade_timestamps = [
            ts for ts in ms.trade_timestamps if ts > cutoff]

        # Log trade activity when trades arrive
        if new_trades:
            total_vol = sum(float(t.get("count_fp", 0) or 0) for t in new_trades)
            mode = "LIVE" if ms.is_live_game else "PRE"
            print(f"    TRADES {ms.ticker}: {len(new_trades)} new, "
                  f"vol={total_vol:.0f} mode={mode}")

        # Drain queues — only count trades after each order's placement
        for order in (ms.yes_order, ms.no_order):
            if order is None or order.remaining <= 0:
                continue
            placed_iso = order.placed_at.strftime("%Y-%m-%dT%H:%M:%S")
            relevant = [t for t in new_trades
                        if t.get("created_time", "")[:19] >= placed_iso]
            d = drain_queue(order, relevant)
            if d > 0:
                print(f"    DRAIN {ms.ticker} {order.side}@{order.price}: "
                      f"drain={d} qpos={order.queue_pos}→"
                      f"{max(0, order.queue_pos - d)}")
                filled = process_fills(order, d)
                if filled > 0:
                    print(f"    FILL {ms.ticker} {order.side}@{order.price}: "
                          f"{filled} contracts filled!")
                    self._record_fill(ms, order, filled, best_yes_bid,
                                      best_no_bid)
                    # Post-fill cooldown (30s in live-game, 0 in pre-game)
                    cooldown = ms.post_fill_cooldown_s
                    if cooldown > 0:
                        ms.paused_until = now + timedelta(seconds=cooldown)
                        print(f"    COOLDOWN {ms.ticker}: {cooldown}s "
                              f"post-fill pause (live-game)")

        # Track trade volume at our price levels for snapshot
        ms.trade_volume_1min += sum(
            int(float(t.get("count_fp", 0) or 0))
            for t in new_trades)

        # -- 4. Pair off matched inventory --
        pairs = pair_off_inventory(ms)
        for p in pairs:
            # Fees already deducted at fill time. Gross P&L from pairing.
            ms.realized_pnl += p["gross_pnl"]
            if p["gross_pnl"] < 0:
                ms.consecutive_losses += 1
            else:
                ms.consecutive_losses = 0
        # Reset oldest_fill_time if inventory fully paired off
        if not ms.yes_queue and not ms.no_queue:
            ms.oldest_fill_time = None
            ms.skew_activated_at = None

        # Update unrealized (conservative: use bid prices, not midpoint)
        ms.unrealized_pnl = unrealized_pnl_cents(
            ms.yes_queue, ms.no_queue, best_yes_bid, best_no_bid)

        # Update peak
        total = self.gs.total_pnl
        if total > self.gs.peak_total_pnl:
            self.gs.peak_total_pnl = total

        # -- 5. Risk checks (layers 2-3) --
        actions = [Action.CONTINUE]
        l2 = check_layer2(ms, max_inventory=self.max_inventory)
        if l2 != Action.CONTINUE:
            actions.append(l2)
            self._log_event(ms, 2, l2, f"net_inv={ms.net_inventory}")
        l3 = check_layer3(ms, self.gs)
        if l3 != Action.CONTINUE:
            actions.append(l3)
            self._log_event(ms, 3, l3,
                            f"rpnl={ms.realized_pnl:.1f} "
                            f"consec={ms.consecutive_losses}")

        action = highest_priority(actions)

        if action == Action.FULL_STOP:
            reason = f"FULL_STOP triggered by {ms.ticker} (L2/L3)"
            for m in self.gs.markets.values():
                self._cancel_orders(m, "full_stop")
                m.active = False
                m.deactivation_reason = reason
                if m.ticker != ms.ticker:
                    self._log_event(m, 4, Action.FULL_STOP,
                                    f"collateral shutdown: {reason}")
            return
        if action == Action.EXIT_MARKET:
            self._cancel_orders(ms, "exit_market")
            ms.active = False
            ms.deactivation_reason = f"EXIT_MARKET (L2/L3)"
            return
        if action == Action.PAUSE_30MIN:
            apply_pause_30min(ms)
            self._cancel_orders(ms, "pause_30min")
            return
        if action in (Action.STOP_AND_FLATTEN, Action.FORCE_CLOSE):
            self._cancel_orders(ms, "flatten")
            self._aggress_flatten(ms, best_yes_bid, yes_ask,
                                  best_no_bid, midpoint)
            return
        if action == Action.AGGRESS_FLATTEN:
            self._aggress_flatten(ms, best_yes_bid, yes_ask,
                                  best_no_bid, midpoint)

        # -- 6. Place/update simulated orders (continuous skew always active) --
        if action <= Action.AGGRESS_FLATTEN:
            self._manage_quotes(ms, best_yes_bid, best_no_bid,
                                yes_ask, midpoint, yes_bids, no_bids,
                                time_soft_close=time_soft_close)

        # -- 7. Snapshot every 6th tick (~60s) --
        self.tick_count += 1
        if self.tick_count % 6 == 0:
            self._write_snapshot(ms, best_yes_bid, yes_ask, spread,
                                 midpoint)
            ms.trade_volume_1min = 0  # reset after snapshot

        # -- 8. Check market resolution (every 6th tick) --
        if self.tick_count % 6 == 0:
            self._check_resolution(ms)

        # Terminal output
        q_yes = ms.yes_order.queue_pos if ms.yes_order else "-"
        q_no = ms.no_order.queue_pos if ms.no_order else "-"
        short = ms.ticker.replace("KXGREENLAND", "GRNLND").replace(
            "KXTRUMPREMOVE", "RMVTRMP").replace(
            "KXGREENLANDPRICE-29JAN21-NOACQ", "GRNLND-NO").replace(
            "KXVPRESNOMR-28-MR", "RUBIOVP").replace(
            "KXINSURRECTION-29-27", "INSURRCT")
        ts = now.strftime("%H:%M:%S")
        print(f"  [{ts}] {short:12s} mid={midpoint:.0f}c sprd={spread} "
              f"q_yes={q_yes} q_no={q_no} inv={ms.net_inventory} "
              f"pnl={ms.realized_pnl:.1f}c")

    # -- Internal helpers --------------------------------------------------

    def _record_fill(self, ms: MarketState, order: SimOrder,
                     filled: int, best_yes_bid: int, best_no_bid: int):
        """Record a simulated maker fill."""
        now = datetime.now(timezone.utc)
        fee = maker_fee_cents(order.price, filled)
        ms.total_fees += fee
        ms.realized_pnl -= fee  # fees reduce P&L immediately

        side_str = f"{order.side}_bid"
        if order.side == "yes":
            ms.yes_queue.extend([order.price] * filled)
        else:
            ms.no_queue.extend([order.price] * filled)

        # Track oldest fill time for L2 time-based checks
        if ms.oldest_fill_time is None:
            ms.oldest_fill_time = now

        inv = ms.net_inventory
        queue_time = (now - order.placed_at).total_seconds()

        try:
            self.db.insert_fill(
                order_id=order.db_id, ticker=ms.ticker, side=side_str,
                price=order.price, size=filled, fee=fee, is_taker=0,
                inventory_after=inv, filled_at=now.isoformat())
            if order.db_id:
                updates = {"remaining": order.remaining,
                           "time_in_queue_s": queue_time}
                if order.remaining == 0:
                    updates["status"] = "filled"
                    updates["filled_at"] = now.isoformat()
                else:
                    updates["status"] = "partial"
                self.db.update_order(order.db_id, **updates)
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1
            print(f"  DB ERROR: {e}", file=sys.stderr)

        tag = "MAKER"
        print(f"  >>> FILL [{tag}] {side_str} {filled}@{order.price}c "
              f"fee={fee:.2f}c inv={inv} pnl={ms.realized_pnl:.1f}c "
              f"queue_time={queue_time:.0f}s")
        discord_notify(
            f"**Paper MM Fill** [{tag}] {ms.ticker} {side_str} "
            f"{filled}@{order.price}c | inv={inv} | "
            f"pnl={ms.realized_pnl:.1f}c")

    def _aggress_flatten(self, ms: MarketState, best_yes_bid: int,
                         yes_ask: int, best_no_bid: int,
                         midpoint: float):
        """Cross the spread to reduce inventory."""
        now = datetime.now(timezone.utc)
        net = ms.net_inventory
        if net == 0:
            return

        if net > 0:
            # Long YES -> buy NO to flatten
            price = 100 - best_yes_bid  # NO ask
            side_str = "no_aggress"
            size = min(self.order_size, abs(net))
            fee = taker_fee_cents(price, size)
            ms.no_queue.extend([price] * size)
            # Cooldown: halt YES quoting for 30s (prevents re-fill cycle)
            ms.aggress_cooldown_yes = now + timedelta(seconds=30)
        else:
            # Long NO -> buy YES to flatten
            price = yes_ask
            side_str = "yes_aggress"
            size = min(self.order_size, abs(net))
            fee = taker_fee_cents(price, size)
            ms.yes_queue.extend([price] * size)
            # Cooldown: halt NO quoting for 30s
            ms.aggress_cooldown_no = now + timedelta(seconds=30)

        ms.total_fees += fee
        ms.realized_pnl -= fee
        inv = ms.net_inventory

        try:
            self.db.insert_fill(
                order_id=None, ticker=ms.ticker, side=side_str,
                price=price, size=size, fee=fee, is_taker=1,
                inventory_after=inv, filled_at=now.isoformat())
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1
            print(f"  DB ERROR: {e}", file=sys.stderr)

        print(f"  >>> FILL [TAKER] {side_str} {size}@{price}c "
              f"fee={fee:.2f}c inv={inv} pnl={ms.realized_pnl:.1f}c")
        discord_notify(
            f"**Paper MM Aggress** {ms.ticker} {side_str} "
            f"{size}@{price}c | inv={inv}")

    def _manage_quotes(self, ms: MarketState, best_yes_bid: int,
                       best_no_bid: int, yes_ask: int, midpoint: float,
                       yes_bids: list, no_bids: list,
                       time_soft_close: bool = False):
        """Place or update simulated resting orders.

        Uses continuous inventory skew (always active, proportional to
        net_inventory) instead of binary threshold.
        """
        now = datetime.now(timezone.utc)
        net_inventory = ms.net_inventory

        # -- Soft-close: reduce-only quoting when freq 30-50 or time-based --
        if ms.is_soft_close or time_soft_close:
            if net_inventory == 0:
                # Flat — cancel both, don't risk new inventory
                self._cancel_orders(ms, "soft_close_flat")
                return

            # Cancel the side that would INCREASE inventory
            if net_inventory > 0:
                self._cancel_order(ms, "yes", "soft_close_reduce")
                reduce_side = "no"
                reduce_bid = best_no_bid
            else:
                self._cancel_order(ms, "no", "soft_close_reduce")
                reduce_side = "yes"
                reduce_bid = best_yes_bid

            # Time-based soft close: aggressive flatten only if |inv| > max_unhedged_exit
            if time_soft_close and abs(net_inventory) > self.max_unhedged_exit:
                aggressive_price = soft_close_exit_price(
                    side=reduce_side, fair_value=midpoint if reduce_side == "yes"
                    else (100 - midpoint),
                    best_bid=reduce_bid, max_slippage=5)

                order = ms.yes_order if reduce_side == "yes" else ms.no_order
                if order is None or order.remaining <= 0 or \
                        abs(order.price - aggressive_price) > 1:
                    self._cancel_order(ms, reduce_side, "soft_close_aggr")
                    bids = yes_bids if reduce_side == "yes" else no_bids
                    queue_pos = sum(q for p, q in bids
                                    if p == aggressive_price)
                    if queue_pos == 0:
                        queue_pos = 10  # small fallback — aggressive placement
                    new_order = SimOrder(
                        side=reduce_side, price=aggressive_price,
                        size=min(self.order_size, abs(net_inventory)),
                        remaining=min(self.order_size, abs(net_inventory)),
                        queue_pos=queue_pos, placed_at=now)
                    if reduce_side == "yes":
                        ms.yes_order = new_order
                    else:
                        ms.no_order = new_order
                    print(f"    SOFT-EXIT {ms.ticker}: aggressive {reduce_side}"
                          f"@{aggressive_price}c (inv={net_inventory}, "
                          f"target<={self.max_unhedged_exit})",
                          flush=True)
            return

        # Dynamic spread from realized volatility
        market_spread = yes_ask - best_yes_bid
        vol_offset = dynamic_spread(ms.midpoint_history, now) - market_spread
        vol_offset = max(0, vol_offset)  # only widen, never tighten below market

        # Continuous skew: gamma=0.5c per contract of inventory
        yes_quote, no_quote = skewed_quotes(
            fair=midpoint, best_yes_bid=best_yes_bid,
            best_no_bid=best_no_bid,
            net_inventory=ms.net_inventory, gamma=0.5,
            quote_offset=vol_offset)

        for side, quote_price, best_bid, bids in [
                ("yes", yes_quote, best_yes_bid, yes_bids),
                ("no", no_quote, best_no_bid, no_bids)]:
            # Post-AGGRESS_FLATTEN cooldown: halt quoting on triggering side
            if is_side_cooled_down(ms, side, now):
                self._cancel_order(ms, side, "aggress_cooldown")
                continue

            # Single-side inventory cap
            if should_skip_side(side, ms.net_inventory,
                                self.max_inventory):
                self._cancel_order(ms, side, "inv_cap")
                continue

            order = ms.yes_order if side == "yes" else ms.no_order

            # Requote if order is stale (>2c from target price)
            if order and abs(order.price - quote_price) > 2:
                self._cancel_order(ms, side, "requote")
                order = None

            if order is None or order.remaining <= 0:
                # Clamp size so fill can't overshoot max inventory
                size = clamp_order_size(side, net_inventory,
                                        self.order_size,
                                        self.max_inventory)
                if size <= 0:
                    continue

                # Layer 1 validation
                rejection = check_layer1(quote_price, size,
                                         midpoint, side=side)
                if rejection:
                    continue

                # Actual depth at this price from the already-fetched book
                queue_pos = sum(q for p, q in bids if p == best_bid)
                if queue_pos == 0:
                    queue_pos = 50  # fallback

                new_order = SimOrder(
                    side=side, price=quote_price, size=size,
                    remaining=size, queue_pos=queue_pos,
                    placed_at=now)

                try:
                    db_id = self.db.insert_order(
                        ms.ticker, side, quote_price, size,
                        size, queue_pos, "resting",
                        now.isoformat())
                    new_order.db_id = db_id
                    self.gs.db_error_count = 0
                except Exception as e:
                    self.gs.db_error_count += 1
                    print(f"  DB ERROR: {e}", file=sys.stderr)

                if side == "yes":
                    ms.yes_order = new_order
                else:
                    ms.no_order = new_order

    def _cancel_orders(self, ms: MarketState, reason: str):
        """Cancel all resting orders for a market."""
        for side in ("yes", "no"):
            self._cancel_order(ms, side, reason)

    def _cancel_order(self, ms: MarketState, side: str, reason: str):
        """Cancel one resting order."""
        order = ms.yes_order if side == "yes" else ms.no_order
        if order is None:
            return
        now = datetime.now(timezone.utc)
        if order.db_id:
            try:
                self.db.update_order(order.db_id,
                                     status="cancelled",
                                     cancelled_at=now.isoformat(),
                                     cancel_reason=reason)
                self.gs.db_error_count = 0
            except Exception as e:
                self.gs.db_error_count += 1
        if side == "yes":
            ms.yes_order = None
        else:
            ms.no_order = None

    def _log_event(self, ms: MarketState, layer: int,
                   action: Action, reason: str):
        """Log a risk event to DB and terminal."""
        now = datetime.now(timezone.utc)
        mid = (ms.midpoint_history[-1][1]
               if ms.midpoint_history else 0)
        print(f"  !!! RISK [L{layer}] {action.name}: {reason}")
        try:
            self.db.insert_event(
                now.isoformat(), ms.ticker, layer, action.name, reason,
                net_inventory=ms.net_inventory,
                realized_pnl=ms.realized_pnl,
                unrealized_pnl=ms.unrealized_pnl,
                midpoint=mid, spread=0,
                consecutive_losses=ms.consecutive_losses)
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1

        # Discord only for critical events (not SKIP_TICK, PAUSE_60S, etc.)
        _discord_actions = {
            Action.FULL_STOP, Action.PAUSE_30MIN, Action.EXIT_MARKET,
        }
        if action in _discord_actions:
            discord_notify(
                f"**Paper MM Risk** [{action.name}] {ms.ticker}: {reason}")

    def _write_snapshot(self, ms: MarketState, best_yes_bid: int,
                        yes_ask: int, spread: int, midpoint: float):
        """Write periodic state snapshot."""
        now = datetime.now(timezone.utc)
        try:
            self.db.insert_snapshot(
                ts=now.isoformat(), ticker=ms.ticker,
                best_yes_bid=best_yes_bid, yes_ask=yes_ask,
                spread=spread, midpoint=midpoint,
                net_inventory=ms.net_inventory,
                yes_held=len(ms.yes_queue),
                no_held=len(ms.no_queue),
                realized_pnl=ms.realized_pnl,
                unrealized_pnl=ms.unrealized_pnl,
                total_pnl=ms.realized_pnl + ms.unrealized_pnl,
                total_fees=ms.total_fees,
                yes_order_price=(ms.yes_order.price
                                 if ms.yes_order else None),
                yes_queue_pos=(ms.yes_order.queue_pos
                               if ms.yes_order else None),
                no_order_price=(ms.no_order.price
                                if ms.no_order else None),
                no_queue_pos=(ms.no_order.queue_pos
                              if ms.no_order else None),
                trade_volume_1min=ms.trade_volume_1min,
                global_realized_pnl=self.gs.total_realized_pnl,
                global_unrealized_pnl=self.gs.total_unrealized_pnl,
                global_total_pnl=self.gs.total_pnl)
            self.gs.db_error_count = 0
        except Exception as e:
            self.gs.db_error_count += 1
            print(f"  DB ERROR: {e}", file=sys.stderr)

    def _check_resolution(self, ms: MarketState):
        """Check if market has resolved (once per minute)."""
        try:
            data = self.client.get_market(ms.ticker)
            market = data.get("market", data)
            result = market.get("result", "")
            if result in ("yes", "no"):
                self._settle_market(ms, result)
        except Exception:
            pass  # non-critical, skip silently

    def _settle_market(self, ms: MarketState, result: str):
        """Settle all inventory on market resolution."""
        now = datetime.now(timezone.utc)
        for side_name, queue in [("yes", ms.yes_queue), ("no", ms.no_queue)]:
            for cost in queue:
                settle_price = 100 if result == side_name else 0
                pnl = settle_price - cost
                ms.realized_pnl += pnl
                try:
                    self.db.insert_fill(
                        order_id=None, ticker=ms.ticker, side="settlement",
                        price=settle_price, size=1, fee=0, is_taker=0,
                        inventory_after=0, filled_at=now.isoformat(),
                        pair_pnl=pnl)
                except Exception:
                    pass
            queue.clear()
        reason = f"market resolved: {result}"
        ms.active = False
        ms.deactivation_reason = reason
        self._cancel_orders(ms, "market_resolved")
        self._log_event(ms, 4, Action.EXIT_MARKET, reason)
        print(f"  *** MARKET RESOLVED: {ms.ticker} -> {result}")
