# src/mm/engine.py
"""Core engine for the paper market maker."""

from __future__ import annotations
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from src.mm.state import (
    SimOrder, MarketState, GlobalState,
    maker_fee_cents, taker_fee_cents, unrealized_pnl_cents,
)
from src.mm.risk import Action, check_layer1, check_layer2, check_layer3, check_layer4, highest_priority
from src.mm.db import MMDatabase
from src.kalshi_client import KalshiClient

import requests as _requests  # for discord

logger = logging.getLogger(__name__)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")


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
                 global_state: GlobalState, order_size: int = 2):
        self.client = client
        self.db = db
        self.gs = global_state
        self.order_size = order_size
        self.tick_count = 0  # per-market tick counter (for snapshot every 6th)

    def tick_one_market(self, ms: MarketState):
        """Execute one tick cycle for a single market."""
        now = datetime.now(timezone.utc)

        # Check pause
        if ms.paused_until and now < ms.paused_until:
            return

        # -- 1. Fetch book + trades --
        try:
            book_data = self.client.get_orderbook(ms.ticker, depth=20)
            trade_data = self.client.get_trades(ms.ticker, limit=500)
            ms.last_api_success = now
        except _requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code in (401, 403, 404):
                self._log_event(ms, 4, Action.EXIT_MARKET,
                                f"fatal HTTP {code}")
                ms.active = False
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
            return

        # Convert to [price_cents, quantity] integer pairs
        yes_bids = [[round(float(p) * 100), int(float(q))]
                     for p, q in yes_bids_raw]
        no_bids = [[round(float(p) * 100), int(float(q))]
                    for p, q in no_bids_raw]

        best_yes_bid = yes_bids[-1][0]
        best_no_bid = no_bids[-1][0]
        yes_ask = 100 - best_no_bid
        spread = yes_ask - best_yes_bid
        midpoint = (best_yes_bid + yes_ask) / 2

        # Update midpoint history (keep last 7 entries ~70s)
        ms.midpoint_history.append((now, midpoint))
        if len(ms.midpoint_history) > 7:
            ms.midpoint_history.pop(0)

        # -- 2. Layer 4 system checks --
        l4 = check_layer4(ms, spread, self.gs.db_error_count)
        if l4 != Action.CONTINUE:
            self._log_event(ms, 4, l4,
                            f"spread={spread} mid={midpoint:.1f}")
            if l4 == Action.PAUSE_60S:
                ms.paused_until = now + timedelta(seconds=60)
            elif l4 >= Action.CANCEL_ALL:
                self._cancel_orders(ms, "risk_l4")
                if l4 == Action.FULL_STOP:
                    for m in self.gs.markets.values():
                        m.active = False
                else:
                    ms.active = False
            return

        # -- 3. Filter new trades & drain queues --
        # Use created_time for dedup (trade_id is UUID, not chronological)
        all_trades = trade_data.get("trades", [])
        new_trades = [t for t in all_trades
                      if t.get("created_time", "") > ms.last_seen_trade_id]
        if new_trades:
            ms.last_seen_trade_id = max(
                t.get("created_time", "") for t in new_trades)

        # Drain queues — only count trades after each order's placement
        for order in (ms.yes_order, ms.no_order):
            if order is None or order.remaining <= 0:
                continue
            placed_iso = order.placed_at.strftime("%Y-%m-%dT%H:%M:%S")
            relevant = [t for t in new_trades
                        if t.get("created_time", "")[:19] >= placed_iso]
            d = drain_queue(order, relevant)
            if d > 0:
                filled = process_fills(order, d)
                if filled > 0:
                    self._record_fill(ms, order, filled, best_yes_bid,
                                      best_no_bid)

        # Track trade volume at our price levels for snapshot
        ms.trade_volume_1min = sum(
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
        l2 = check_layer2(ms)
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
            for m in self.gs.markets.values():
                self._cancel_orders(m, "full_stop")
                m.active = False
            return
        if action == Action.EXIT_MARKET:
            self._cancel_orders(ms, "exit_market")
            ms.active = False
            return
        if action == Action.PAUSE_30MIN:
            ms.paused_until = now + timedelta(minutes=30)
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

        # Track skew activation for 1-hour escalation
        if action == Action.SKEW_QUOTES:
            if ms.skew_activated_at is None:
                ms.skew_activated_at = now
        elif abs(ms.net_inventory) <= 10:
            ms.skew_activated_at = None  # reset when inventory normalizes

        # -- 6. Place/update simulated orders --
        if action <= Action.AGGRESS_FLATTEN:
            skew = action == Action.SKEW_QUOTES
            self._manage_quotes(ms, best_yes_bid, best_no_bid,
                                yes_ask, midpoint, yes_bids, no_bids,
                                skew=skew)

        # -- 7. Snapshot every 6th tick (~60s) --
        self.tick_count += 1
        if self.tick_count % 6 == 0:
            self._write_snapshot(ms, best_yes_bid, yes_ask, spread,
                                 midpoint)

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
        else:
            # Long NO -> buy YES to flatten
            price = yes_ask
            side_str = "yes_aggress"
            size = min(self.order_size, abs(net))
            fee = taker_fee_cents(price, size)
            ms.yes_queue.extend([price] * size)

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
                       skew: bool = False):
        """Place or update simulated resting orders.

        If skew=True (inventory > 10), adjust prices to attract
        offsetting flow rather than crossing the spread.
        """
        now = datetime.now(timezone.utc)
        net = ms.net_inventory  # positive = long YES

        for side, best_bid, bids in [("yes", best_yes_bid, yes_bids),
                                     ("no", best_no_bid, no_bids)]:
            order = ms.yes_order if side == "yes" else ms.no_order
            quote_price = best_bid

            # Inventory skewing: adjust quotes to attract offsetting flow
            if skew and net != 0:
                if net > 0:
                    # Long YES: lower YES bid (buy less), lower NO bid
                    if side == "yes":
                        quote_price = max(1, best_bid - 2)
                    else:
                        quote_price = max(1, best_bid - 1)
                else:
                    # Long NO: lower NO bid (buy less), lower YES bid
                    if side == "no":
                        quote_price = max(1, best_bid - 2)
                    else:
                        quote_price = max(1, best_bid - 1)

            # Requote if order is stale (>2c from target price)
            if order and abs(order.price - quote_price) > 2:
                self._cancel_order(ms, side, "requote")
                order = None

            if order is None or order.remaining <= 0:
                # Layer 1 validation
                rejection = check_layer1(quote_price, self.order_size,
                                         midpoint, side=side)
                if rejection:
                    continue

                # Actual depth at this price from the already-fetched book
                queue_pos = sum(q for p, q in bids if p == best_bid)
                if queue_pos == 0:
                    queue_pos = 50  # fallback

                new_order = SimOrder(
                    side=side, price=quote_price, size=self.order_size,
                    remaining=self.order_size, queue_pos=queue_pos,
                    placed_at=now)

                try:
                    db_id = self.db.insert_order(
                        ms.ticker, side, quote_price, self.order_size,
                        self.order_size, queue_pos, "resting",
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

        if layer >= 2:
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
        # Settle YES inventory
        for cost in ms.yes_queue:
            settle_price = 100 if result == "yes" else 0
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
        # Settle NO inventory
        for cost in ms.no_queue:
            settle_price = 100 if result == "no" else 0
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

        ms.yes_queue.clear()
        ms.no_queue.clear()
        ms.active = False
        self._cancel_orders(ms, "market_resolved")
        self._log_event(ms, 4, Action.EXIT_MARKET,
                        f"market resolved: {result}")
        print(f"  *** MARKET RESOLVED: {ms.ticker} -> {result}")
