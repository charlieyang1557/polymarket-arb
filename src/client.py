"""
Polymarket API wrapper.
- Gamma API: market metadata, events
- CLOB API (py-clob-client): prices, order books
"""

import json
import logging
import time
from functools import wraps
from typing import Optional

import requests

from config.settings import CLOB_API_BASE, GAMMA_API_BASE, METADATA_CACHE_TTL_SECONDS
from src.models import Event, Market, Outcome

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------

_request_times: list[float] = []
MAX_REQUESTS_PER_MINUTE = 60


def _rate_limit():
    now = time.time()
    _request_times[:] = [t for t in _request_times if now - t < 60]
    if len(_request_times) >= MAX_REQUESTS_PER_MINUTE:
        sleep_for = 60 - (now - _request_times[0])
        logger.debug("Rate limit reached, sleeping %.1fs", sleep_for)
        time.sleep(sleep_for)
    _request_times.append(time.time())


def _with_retry(max_attempts: int = 3, backoff: float = 1.0):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except requests.RequestException as exc:
                    if attempt == max_attempts - 1:
                        raise
                    wait = backoff * (2 ** attempt)
                    logger.warning("Request failed (%s), retrying in %.1fs", exc, wait)
                    time.sleep(wait)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# PolymarketClient
# ---------------------------------------------------------------------------

class PolymarketClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # Metadata cache
        self._events_cache: list[Event] = []
        self._cache_fetched_at: float = 0.0

    # ------------------------------------------------------------------
    # Gamma API
    # ------------------------------------------------------------------

    @_with_retry()
    def _gamma_get(self, path: str, params: Optional[dict] = None) -> dict | list:
        _rate_limit()
        url = f"{GAMMA_API_BASE}{path}"
        resp = self._session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_clob_token_ids(raw) -> list[str]:
        """Gamma API returns clobTokenIds as a JSON-encoded string, not a list."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def get_all_active_events(self, force_refresh: bool = False) -> list[Event]:
        """Fetch all active events with their markets. Cached for METADATA_CACHE_TTL_SECONDS."""
        age = time.time() - self._cache_fetched_at
        if not force_refresh and self._events_cache and age < METADATA_CACHE_TTL_SECONDS:
            return self._events_cache

        raw_events = self._gamma_get("/events", params={"active": "true", "closed": "false", "limit": 500})
        events = []
        for raw in raw_events:
            markets = []
            for rm in raw.get("markets", []):
                outcomes = []
                for token in self._parse_clob_token_ids(rm.get("clobTokenIds", "[]")):
                    outcomes.append(Outcome(token_id=token, name="", best_ask=0.0, best_bid=0.0))
                markets.append(Market(
                    market_id=rm.get("id", ""),
                    question=rm.get("question", ""),
                    event_id=str(raw.get("id", "")),
                    event_slug=raw.get("slug", ""),
                    outcomes=outcomes,
                    active=rm.get("active", True),
                    neg_risk=rm.get("negRisk", False),
                    volume_24h=float(rm.get("volumeNum", 0) or rm.get("volume24hr", 0) or 0),
                ))
            events.append(Event(
                event_id=str(raw.get("id", "")),
                title=raw.get("title", ""),
                category=raw.get("category", ""),
                markets=markets,
                active=raw.get("active", True),
            ))

        self._events_cache = events
        self._cache_fetched_at = time.time()
        logger.info("Fetched %d active events from Gamma API", len(events))
        return events

    def get_event_markets(self, event_id: str) -> list[Market]:
        raw = self._gamma_get(f"/events/{event_id}")
        markets = []
        for rm in raw.get("markets", []):
            outcomes = [
                Outcome(token_id=t, name="", best_ask=0.0, best_bid=0.0)
                for t in self._parse_clob_token_ids(rm.get("clobTokenIds", "[]"))
            ]
            markets.append(Market(
                market_id=rm.get("id", ""),
                question=rm.get("question", ""),
                event_id=event_id,
                event_slug=raw.get("slug", ""),
                outcomes=outcomes,
                active=rm.get("active", True),
                neg_risk=rm.get("negRisk", False),
                volume_24h=float(rm.get("volumeNum", 0) or rm.get("volume24hr", 0) or 0),
            ))
        return markets

    # ------------------------------------------------------------------
    # CLOB API (prices & order books via direct REST — no auth required)
    # ------------------------------------------------------------------

    @_with_retry()
    def _clob_get(self, path: str, params: Optional[dict] = None) -> dict | list:
        _rate_limit()
        url = f"{CLOB_API_BASE}{path}"
        resp = self._session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Return best ask (BUY) or best bid (SELL) for a token."""
        try:
            data = self._clob_get("/price", params={"token_id": token_id, "side": side})
            return float(data.get("price", 0))
        except Exception as exc:
            logger.warning("get_price failed for %s: %s", token_id, exc)
            return None

    def get_prices(self, token_ids: list[str]) -> dict[str, dict[str, float]]:
        """
        Batch fetch best ask and best bid for multiple tokens.
        Returns {token_id: {"ask": float, "bid": float}}
        """
        results: dict[str, dict[str, float]] = {}
        for token_id in token_ids:
            ask = self.get_price(token_id, "BUY")
            bid = self.get_price(token_id, "SELL")
            results[token_id] = {"ask": ask or 0.0, "bid": bid or 0.0}
        return results

    def get_order_book(self, token_id: str) -> dict:
        """Return raw order book: {bids: [[price, size], ...], asks: [...]}"""
        try:
            return self._clob_get("/book", params={"token_id": token_id})
        except Exception as exc:
            logger.warning("get_order_book failed for %s: %s", token_id, exc)
            return {"bids": [], "asks": []}

    def get_book_depth(self, token_id: str, side: str, amount_usd: float) -> dict:
        """
        Calculate average fill price for `amount_usd` into the order book.
        side: "BUY" (walks asks) or "SELL" (walks bids).
        Returns {"avg_price": float, "filled_usd": float, "slippage_pct": float}
        """
        book = self.get_order_book(token_id)
        levels = book.get("asks" if side == "BUY" else "bids", [])

        remaining = amount_usd
        total_cost = 0.0
        best_price: Optional[float] = None

        for level in levels:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            level_cost = price * size

            if best_price is None:
                best_price = price

            if remaining <= level_cost:
                total_cost += remaining
                remaining = 0
                break
            else:
                total_cost += level_cost
                remaining -= level_cost

        if amount_usd == 0 or best_price is None:
            return {"avg_price": 0.0, "filled_usd": 0.0, "slippage_pct": 0.0}

        filled = amount_usd - remaining
        avg_price = total_cost / filled if filled > 0 else 0.0
        slippage = abs(avg_price - best_price) / best_price * 100 if best_price else 0.0

        return {
            "avg_price": avg_price,
            "filled_usd": filled,
            "slippage_pct": slippage,
        }

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            data = self._clob_get("/midpoint", params={"token_id": token_id})
            return float(data.get("mid", 0))
        except Exception as exc:
            logger.warning("get_midpoint failed for %s: %s", token_id, exc)
            return None
