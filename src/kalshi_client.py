"""
Kalshi API wrapper — thin client with RSA-PSS auth.

Bypasses the buggy kalshi_python_sync SDK. Uses raw HTTP
requests with RSA-PSS signing, same pattern as src/client.py.
"""

import base64
import json
import logging
import time
from typing import Any

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, api_key: str, private_key_path: str,
                 base_url: str = DEMO_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        with open(private_key_path, "r") as f:
            pem_data = f.read().encode()
        self.private_key = serialization.load_pem_private_key(
            pem_data, password=None, backend=default_backend()
        )

    # -- Auth ---------------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: dict | None = None,
                 body: dict | None = None) -> dict:
        url = self.base_url + path
        headers = self._auth_headers(method, path)
        resp = requests.request(
            method, url, headers=headers, params=params,
            json=body, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body=body)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # -- Markets ------------------------------------------------------------

    def get_markets(self, *, limit: int = 100, status: str | None = None,
                    cursor: str | None = None, **kw) -> dict:
        params = {"limit": limit, **kw}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 20) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook",
                             params={"depth": depth})

    def get_trades(self, ticker: str, limit: int = 50,
                   min_ts: int | None = None) -> dict:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._request("GET", "/markets/trades", params=params)

    def get_candlesticks(self, tickers: str | list[str], *,
                         period_interval: int = 60,
                         start_ts: int | None = None,
                         end_ts: int | None = None) -> dict:
        if isinstance(tickers, list):
            tickers = ",".join(tickers)
        params: dict[str, Any] = {
            "market_tickers": tickers,
            "period_interval": period_interval,
        }
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        return self._request("GET", "/markets/candlesticks", params=params)

    # -- Events -------------------------------------------------------------

    def get_events(self, *, limit: int = 20,
                   with_nested_markets: bool = True,
                   status: str | None = None, **kw) -> dict:
        params = {"limit": limit,
                   "with_nested_markets": with_nested_markets, **kw}
        if status:
            params["status"] = status
        return self._request("GET", "/events", params=params)

    def get_event(self, event_ticker: str) -> dict:
        return self._request("GET", f"/events/{event_ticker}",
                             params={"with_nested_markets": True})

    # -- Orders (Phase 3+) --------------------------------------------------

    def place_order(self, ticker: str, *, side: str, price: int,
                    count: int, order_type: str = "limit") -> dict:
        return self.post("/orders", {
            "ticker": ticker,
            "side": side,
            "type": order_type,
            "yes_price": price,
            "count": count,
        })

    def cancel_order(self, order_id: str) -> dict:
        return self.delete(f"/orders/{order_id}")

    # -- Account ------------------------------------------------------------

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")
