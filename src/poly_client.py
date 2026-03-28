"""
Polymarket US API wrapper — Adapter for the MM engine.

Translation layer between polymarket-us SDK responses and the
format expected by src/mm/engine.py, state.py, and risk.py.

Architecture: the engine calls methods identical to KalshiClient.
This client normalizes Polymarket responses to match Kalshi's format.

Key conversions:
  - Prices: SDK returns "0.55" → engine expects dollar strings
    in orderbook, then converts to cents internally
  - Orderbook: SDK bids/offers → yes_dollars/no_dollars arrays
  - Fees: Polymarket PAYS makers (negative fee = rebate)
  - Ticker: Polymarket slug used wherever engine expects ticker
"""

import logging
import time
from typing import Any, Optional

from polymarket_us import PolymarketUS

logger = logging.getLogger(__name__)

# Fee structure by category
# Taker fee = taker_fee_pct * P * (1-P) * 100 per contract
# Maker rebate = rebate_pct * taker_fee (returned as negative fee)
FEE_CONFIG = {
    "sports":       {"taker_fee_pct": 0.02, "rebate_pct": 0.25},
    "crypto":       {"taker_fee_pct": 0.02, "rebate_pct": 0.20},
    "geopolitical": {"taker_fee_pct": 0.0,  "rebate_pct": 0.0},   # fee-free
}
DEFAULT_FEE = FEE_CONFIG["sports"]  # platform is currently sports-only


# ---------------------------------------------------------------------------
# Pure normalization functions (tested)
# ---------------------------------------------------------------------------

def normalize_orderbook(raw_book: Optional[dict]) -> dict:
    """Convert SDK book response to engine's expected format.

    Engine expects:
        {"orderbook_fp": {
            "yes_dollars": [[price_str, qty_str], ...],  # sorted ascending
            "no_dollars":  [[price_str, qty_str], ...],  # sorted ascending
        }}

    SDK returns:
        {"marketData": {
            "bids":   [{"px": {"value": "0.55"}, "qty": "100"}, ...],
            "offers": [{"px": {"value": "0.58"}, "qty": "150"}, ...],
        }}

    SDK bids = YES bids (long side).
    SDK offers = YES asks → NO bids at (1 - ask_price).

    Both arrays sorted ascending: worst (lowest) price first, best (highest) last.
    This matches the Kalshi orderbook_fp format the engine parses.
    """
    if raw_book is None:
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}

    md = raw_book.get("marketData") or {}

    # Parse YES bids from SDK bids
    yes_bids = []
    for level in md.get("bids", []):
        try:
            px = float(level["px"]["value"])
            qty = float(level["qty"])
            yes_bids.append((px, qty))
        except (KeyError, ValueError, TypeError):
            continue

    # Sort ascending (engine expects best bid = last element)
    yes_bids.sort(key=lambda x: x[0])

    # Parse NO bids from SDK offers (YES ask → NO bid = 1 - ask)
    no_bids = []
    for level in md.get("offers", []):
        try:
            px = float(level["px"]["value"])
            qty = float(level["qty"])
            no_price = round(1.0 - px, 4)
            if no_price > 0:
                no_bids.append((no_price, qty))
        except (KeyError, ValueError, TypeError):
            continue

    # Sort ascending (engine expects best NO bid = last element)
    no_bids.sort(key=lambda x: x[0])

    return {
        "orderbook_fp": {
            "yes_dollars": [
                [f"{px:.4f}", f"{qty:.0f}"] for px, qty in yes_bids
            ],
            "no_dollars": [
                [f"{px:.4f}", f"{qty:.0f}"] for px, qty in no_bids
            ],
        }
    }


def normalize_bbo(raw_bbo: Optional[dict]) -> dict:
    """Extract key BBO fields with cent conversion."""
    if raw_bbo is None:
        return {"best_bid_cents": 0, "best_ask_cents": 0,
                "last_trade_cents": 0, "shares_traded": 0,
                "open_interest": 0}

    md = raw_bbo.get("marketData") or {}

    def _px_cents(field_name: str) -> int:
        val = md.get(field_name)
        if val and isinstance(val, dict):
            try:
                return round(float(val.get("value", 0)) * 100)
            except (ValueError, TypeError):
                return 0
        return 0

    return {
        "best_bid_cents": _px_cents("bestBid"),
        "best_ask_cents": _px_cents("bestAsk"),
        "last_trade_cents": _px_cents("lastTradePx"),
        "shares_traded": int(float(md.get("sharesTraded", "0") or "0")),
        "open_interest": int(float(md.get("openInterest", "0") or "0")),
    }


def normalize_trades(raw_trades: Optional[list]) -> dict:
    """Normalize trade data to engine format.

    Polymarket US SDK v0.1.2 doesn't expose a direct trades endpoint.
    Trade detection will use BBO changes or WebSocket events.
    Returns empty trades list for now — engine handles this gracefully
    (just won't detect live games via trade frequency).
    """
    return {"trades": []}


def calculate_maker_fee(price_cents: int, category: str = "sports",
                         count: int = 1) -> float:
    """Calculate maker fee for Polymarket US.

    Returns NEGATIVE value (rebate) for sports/crypto markets.
    Returns 0 for fee-free markets (geopolitical).

    Formula:
      taker_fee_per_contract = taker_fee_pct * P * (1-P) * 100
      maker_rebate = -rebate_pct * taker_fee_per_contract * count
    """
    p = price_cents / 100
    pq = p * (1 - p)
    if pq <= 0:
        return 0

    config = FEE_CONFIG.get(category, DEFAULT_FEE)
    taker_fee = config["taker_fee_pct"] * pq * 100
    rebate = config["rebate_pct"] * taker_fee

    return round(-rebate * count, 4)


# ---------------------------------------------------------------------------
# PolyClient — drop-in adapter for KalshiClient
# ---------------------------------------------------------------------------

class PolyClient:
    """Polymarket US client matching KalshiClient interface.

    The engine calls the same method names (get_orderbook, get_trades,
    get_market, place_order, cancel_order). This client translates
    between Polymarket SDK responses and the engine's expected format.
    """

    def __init__(self, key_id: Optional[str] = None,
                 secret_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        if key_id and secret_key:
            self.client = PolymarketUS(
                key_id=key_id, secret_key=secret_key)
            self._authenticated = True
        else:
            self.client = PolymarketUS()
            self._authenticated = False

        self._category = "sports"  # platform is currently sports-only

    # -- Markets (read-only, public) ----------------------------------------

    def get_orderbook(self, slug: str, depth: int = 20) -> dict:
        """Fetch and normalize orderbook.

        Returns Kalshi-compatible format for the engine.
        """
        try:
            raw = self.client.markets.book(slug)
            return normalize_orderbook(raw)
        except Exception as e:
            logger.warning("get_orderbook failed for %s: %s", slug, e)
            return normalize_orderbook(None)

    def get_market(self, slug: str) -> dict:
        """Fetch single market details."""
        try:
            raw = self.client.markets.retrieve_by_slug(slug)
            return {"market": raw}
        except Exception as e:
            logger.warning("get_market failed for %s: %s", slug, e)
            return {}

    def get_trades(self, slug: str, limit: int = 50,
                   min_ts: int | None = None) -> dict:
        """Fetch recent trades.

        SDK v0.1.2 doesn't expose a trades endpoint.
        Returns empty trades list — engine handles gracefully.
        """
        return normalize_trades(None)

    def get_events(self, *, limit: int = 20,
                   with_nested_markets: bool = True,
                   status: str | None = None, **kw) -> dict:
        """List events matching filters."""
        params: dict[str, Any] = {"limit": limit}

        if status == "open":
            params["active"] = True
            params["closed"] = False
        elif status == "settled":
            params["closed"] = True

        if "cursor" in kw:
            params["offset"] = int(kw["cursor"] or 0)

        try:
            resp = self.client.events.list(params)
            events = resp.get("events", [])
            # Return cursor for pagination (offset-based)
            next_offset = (int(kw.get("cursor", 0) or 0)) + limit
            has_more = len(events) >= limit
            return {
                "events": events,
                "cursor": str(next_offset) if has_more else None,
            }
        except Exception as e:
            logger.warning("get_events failed: %s", e)
            return {"events": [], "cursor": None}

    def get_event(self, event_slug: str) -> dict:
        """Fetch single event with nested markets."""
        try:
            raw = self.client.events.retrieve_by_slug(event_slug)
            return {"event": raw}
        except Exception as e:
            logger.warning("get_event failed for %s: %s", event_slug, e)
            return {}

    def get_bbo(self, slug: str) -> dict:
        """Fetch best bid/offer + stats. Polymarket-specific convenience."""
        try:
            raw = self.client.markets.bbo(slug)
            return normalize_bbo(raw)
        except Exception as e:
            logger.warning("get_bbo failed for %s: %s", slug, e)
            return normalize_bbo(None)

    # -- Orders (requires auth) ---------------------------------------------

    def place_order(self, slug: str, *, side: str, price: int,
                    count: int, order_type: str = "limit") -> dict:
        """Place limit order. Requires authenticated client.

        Args:
            slug: Market slug (used as ticker)
            side: "yes" or "no"
            price: Price in cents (1-99)
            count: Number of contracts
            order_type: "limit" (only type supported)

        Raises:
            NotImplementedError: If client not authenticated
        """
        if not self._authenticated:
            raise NotImplementedError(
                "Trading requires authenticated client. "
                "Pass key_id and secret_key to PolyClient()."
            )

        # Convert cents to dollar string for SDK
        price_dollars = price / 100.0

        try:
            # SDK order placement — exact method TBD when we get API keys
            # For now, stub the call to match expected interface
            logger.info("place_order: slug=%s side=%s price=%dc count=%d",
                        slug, side, price, count)
            raise NotImplementedError(
                "Order placement not yet implemented — "
                "waiting for API key configuration"
            )
        except NotImplementedError:
            raise
        except Exception as e:
            logger.error("place_order failed: %s", e)
            raise

    def cancel_order(self, order_id: str) -> dict:
        """Cancel order by ID. Requires authenticated client."""
        if not self._authenticated:
            raise NotImplementedError(
                "Trading requires authenticated client."
            )
        try:
            logger.info("cancel_order: %s", order_id)
            raise NotImplementedError(
                "Order cancellation not yet implemented — "
                "waiting for API key configuration"
            )
        except NotImplementedError:
            raise
        except Exception as e:
            logger.error("cancel_order failed: %s", e)
            raise

    # -- Account (requires auth) -------------------------------------------

    def get_balance(self) -> dict:
        """Get account balance."""
        if not self._authenticated:
            return {"balance": 0}
        try:
            # SDK method TBD
            raise NotImplementedError("Balance check not yet implemented")
        except NotImplementedError:
            raise
        except Exception as e:
            logger.error("get_balance failed: %s", e)
            return {"balance": 0}
