"""
WebSocket Resolution Logger for Polymarket Strategy B exploration.

Connects to Polymarket Market WebSocket and logs all events, with special
attention to market_resolved events and price movements around resolutions.

Diagnostic-first: pure observation, no trading logic.

Usage:
    python scripts/ws_resolution_logger.py                   # Default: 200 tokens
    python scripts/ws_resolution_logger.py --max-tokens 400  # Subscribe to more tokens

Architecture:
    1. Fetch most active open markets from Gamma API (binary Yes/No, by 24h volume)
    2. Subscribe to their CLOB token IDs on the Market WebSocket
    3. Log ALL events to data/ws_logs/YYYY-MM-DD_HHMMSS.jsonl
    4. Print interesting events (resolutions, large price moves) to console
    5. Periodically refresh subscriptions (add newly-closing markets, drop resolved)
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import websockets

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
PING_INTERVAL = 8  # seconds (must be < 10s or server disconnects)
MAX_TOKENS_DEFAULT = 200  # conservative; limit is 500
REFRESH_INTERVAL = 300  # re-fetch closing-soon markets every 5 min
OUTPUT_DIR = os.path.join("data", "ws_logs")

# Rate limiter for Gamma API
_last_request_time = 0.0

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_state = {
    "subscribed_tokens": set(),      # currently subscribed token IDs
    "token_to_market": {},           # token_id -> market metadata
    "events_logged": 0,
    "resolutions_seen": 0,
    "price_changes_seen": 0,
    "last_trade_prices_seen": 0,
    "connection_started": None,
    "log_file": None,
    "running": True,
}


def _log_path():
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(OUTPUT_DIR, f"{ts}.jsonl")


# ---------------------------------------------------------------------------
# Gamma API: fetch markets closing soon
# ---------------------------------------------------------------------------
def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_request_time = time.time()


def fetch_active_markets(max_tokens: int = MAX_TOKENS_DEFAULT) -> dict:
    """Fetch most active open binary markets by 24h volume.

    Returns dict mapping token_id -> market metadata.
    We subscribe to high-volume markets because resolutions can happen
    at any time (not just at endDate), and active markets are most
    likely to resolve or show interesting price dynamics.
    """
    now = datetime.now(timezone.utc)

    token_map = {}
    offset = 0
    page_size = 100  # smaller pages since sorted by volume

    print(f"  Fetching most active open markets (by 24h volume)...")

    while len(token_map) < max_tokens * 2:  # 2 tokens per market (YES + NO)
        _rate_limit()
        params = {
            "closed": "false",
            "limit": page_size,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            print(f"  Gamma API error at offset {offset}: {e}")
            break

        if not markets:
            break

        for m in markets:
            # Filter: binary Yes/No only
            outcomes = m.get("outcomes")
            if outcomes not in ('["Yes", "No"]', '["Yes","No"]'):
                continue

            # Filter: minimum 24h volume (active trading)
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            vol24 = float(m.get("volume24hr") or 0)
            if vol24 < 1000:
                # Sorted by volume desc, so once we hit low volume we're done
                print(f"  Reached low-volume markets at offset {offset}")
                return token_map

            # Parse CLOB token IDs
            clob_raw = m.get("clobTokenIds")
            if not clob_raw:
                continue
            try:
                token_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
            except (json.JSONDecodeError, TypeError):
                continue

            if not token_ids or len(token_ids) < 1:
                continue

            # Parse outcome prices for current snapshot
            prices = [None, None]
            op_raw = m.get("outcomePrices")
            if op_raw:
                try:
                    prices = json.loads(op_raw) if isinstance(op_raw, str) else op_raw
                    prices = [float(p) for p in prices]
                except (json.JSONDecodeError, TypeError, ValueError):
                    prices = [None, None]

            end_date_str = m.get("endDate") or ""

            meta = {
                "condition_id": m.get("conditionId"),
                "question": m.get("question", "")[:200],
                "slug": m.get("slug", ""),
                "end_date": end_date_str,
                "volume": vol,
                "volume_24h": vol24,
                "yes_price": prices[0] if len(prices) > 0 else None,
                "no_price": prices[1] if len(prices) > 1 else None,
                "group_item_title": m.get("groupItemTitle", ""),
                "fetched_at": now.isoformat(),
            }

            for i, tid in enumerate(token_ids):
                outcome = "YES" if i == 0 else "NO"
                token_map[str(tid)] = {**meta, "outcome": outcome, "token_id": str(tid)}

            if len(token_map) >= max_tokens * 2:
                break

        offset += page_size

    return token_map


# ---------------------------------------------------------------------------
# WebSocket event logging
# ---------------------------------------------------------------------------
def _write_event(event_type: str, data: dict):
    """Write event to JSONL log file."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_unix": time.time(),
        "event_type": event_type,
        **data,
    }
    f = _state["log_file"]
    if f:
        f.write(json.dumps(record, default=str) + "\n")
        # Flush every 50 events
        _state["events_logged"] += 1
        if _state["events_logged"] % 50 == 0:
            f.flush()


def _handle_message(raw: str):
    """Parse and log a WebSocket message."""
    if raw == "PONG":
        return

    try:
        msgs = json.loads(raw)
    except json.JSONDecodeError:
        _write_event("unparseable", {"raw": raw[:500]})
        return

    # The WS can send a single message or an array
    if isinstance(msgs, dict):
        msgs = [msgs]

    for msg in msgs:
        event_type = msg.get("event_type", "unknown")
        asset_id = msg.get("asset_id", "")

        # Enrich with market metadata
        market_meta = _state["token_to_market"].get(asset_id, {})
        question = market_meta.get("question", "")

        if event_type == "market_resolved":
            _state["resolutions_seen"] += 1
            winning = msg.get("winning_outcome", "?")
            winning_asset = msg.get("winning_asset_id", "")
            print(f"\n  *** RESOLUTION #{_state['resolutions_seen']} ***")
            print(f"      Market: {question}")
            print(f"      Winner: {winning}")
            print(f"      Winning asset: {winning_asset}")
            print(f"      Time: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
            print(f"      Raw: {json.dumps(msg, default=str)[:300]}")
            _write_event("market_resolved", {
                "raw": msg,
                "question": question,
                "meta": market_meta,
            })

        elif event_type == "price_change":
            _state["price_changes_seen"] += 1
            price = msg.get("price", "?")
            side = msg.get("side", "?")
            size = msg.get("size", "?")
            _write_event("price_change", {
                "asset_id": asset_id,
                "price": price,
                "side": side,
                "size": size,
                "best_bid": msg.get("best_bid"),
                "best_ask": msg.get("best_ask"),
                "question": question[:100],
            })

        elif event_type == "last_trade_price":
            _state["last_trade_prices_seen"] += 1
            _write_event("last_trade_price", {
                "asset_id": asset_id,
                "price": msg.get("price"),
                "side": msg.get("side"),
                "size": msg.get("size"),
                "question": question[:100],
            })

        elif event_type == "book":
            _write_event("book", {
                "asset_id": asset_id,
                "bids_count": len(msg.get("bids", [])),
                "asks_count": len(msg.get("asks", [])),
                "question": question[:100],
            })

        elif event_type == "best_bid_ask":
            _write_event("best_bid_ask", {
                "asset_id": asset_id,
                "best_bid": msg.get("best_bid"),
                "best_ask": msg.get("best_ask"),
                "question": question[:100],
            })

        elif event_type == "new_market":
            print(f"\n  + NEW MARKET: {msg.get('question', '?')[:100]}")
            _write_event("new_market", {"raw": msg})

        elif event_type == "tick_size_change":
            _write_event("tick_size_change", {"raw": msg})

        else:
            _write_event(event_type, {"raw": msg})


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------
_last_status_time = 0.0

def _print_status():
    global _last_status_time
    now = time.time()
    if now - _last_status_time < 30:
        return
    _last_status_time = now

    elapsed = now - (_state["connection_started"] or now)
    mins = elapsed / 60
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
          f"{mins:.0f}min | "
          f"events={_state['events_logged']} | "
          f"resolutions={_state['resolutions_seen']} | "
          f"price_changes={_state['price_changes_seen']} | "
          f"trades={_state['last_trade_prices_seen']} | "
          f"tokens={len(_state['subscribed_tokens'])}")


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------
def _discord_notify(message: str):
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(url, json={"content": message[:1900]}, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------
async def run_logger(max_tokens: int):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_path = _log_path()
    _state["log_file"] = open(log_path, "a")
    _state["connection_started"] = time.time()

    print(f"=== Polymarket WebSocket Resolution Logger ===")
    print(f"  Log file: {log_path}")
    print(f"  Max tokens: {max_tokens}")
    print()

    # Initial fetch of active markets
    token_map = fetch_active_markets(max_tokens)
    _state["token_to_market"] = token_map
    token_ids = list(token_map.keys())[:max_tokens]

    if not token_ids:
        print("  ERROR: No active markets found.")
        return

    print(f"\n  Found {len(token_map)} tokens ({len(token_map)//2} markets)")
    print(f"  Subscribing to {len(token_ids)} tokens...")

    # Show a few sample markets
    seen_questions = set()
    for tid in token_ids[:30]:
        q = token_map[tid]["question"]
        if q not in seen_questions:
            seen_questions.add(q)
            vol24 = token_map[tid].get("volume_24h", 0)
            yp = token_map[tid].get("yes_price", "?")
            print(f"    - (24h=${vol24:,.0f} YES={yp}) {q[:80]}")
            if len(seen_questions) >= 10:
                break
    remaining = len(token_ids) // 2 - len(seen_questions)
    if remaining > 0:
        print(f"    ... and {remaining} more markets")

    print(f"\n  Connecting to WebSocket...")

    last_refresh = time.time()
    retry_count = 0
    max_retries = 20

    while _state["running"] and retry_count < max_retries:
        try:
            async with websockets.connect(WS_URL, ping_interval=None) as ws:
                retry_count = 0  # reset on successful connect
                print(f"  Connected! Sending subscription...")

                # Subscribe
                sub_msg = {
                    "assets_ids": token_ids,
                    "type": "market",
                    "custom_feature_enabled": True,
                }
                await ws.send(json.dumps(sub_msg))
                _state["subscribed_tokens"] = set(token_ids)
                _write_event("subscribed", {
                    "token_count": len(token_ids),
                    "market_count": len(token_ids) // 2,
                })

                print(f"  Subscribed to {len(token_ids)} tokens. Listening...\n")
                _discord_notify(
                    f"WS Logger started: {len(token_ids)} tokens, "
                    f"{len(token_ids)//2} active markets"
                )

                # Ping task
                async def ping_loop():
                    while _state["running"]:
                        try:
                            await ws.send("PING")
                            await asyncio.sleep(PING_INTERVAL)
                        except Exception:
                            break

                ping_task = asyncio.create_task(ping_loop())

                # Refresh task: periodically re-fetch markets and update subscriptions
                async def refresh_loop():
                    nonlocal token_ids, last_refresh
                    while _state["running"]:
                        await asyncio.sleep(REFRESH_INTERVAL)
                        try:
                            new_map = fetch_active_markets(max_tokens)
                            new_ids = set(new_map.keys())
                            old_ids = _state["subscribed_tokens"]

                            to_add = list(new_ids - old_ids)[:50]  # add up to 50 new
                            to_remove = list(old_ids - new_ids)[:50]

                            if to_add:
                                await ws.send(json.dumps({
                                    "assets_ids": to_add,
                                    "operation": "subscribe",
                                    "custom_feature_enabled": True,
                                }))
                                _state["subscribed_tokens"].update(to_add)
                                _state["token_to_market"].update(
                                    {k: v for k, v in new_map.items() if k in to_add}
                                )
                                print(f"  [refresh] +{len(to_add)} tokens subscribed")

                            if to_remove:
                                await ws.send(json.dumps({
                                    "assets_ids": to_remove,
                                    "operation": "unsubscribe",
                                }))
                                _state["subscribed_tokens"] -= set(to_remove)
                                print(f"  [refresh] -{len(to_remove)} tokens unsubscribed")

                            _write_event("refresh", {
                                "added": len(to_add),
                                "removed": len(to_remove),
                                "total_tokens": len(_state["subscribed_tokens"]),
                            })
                        except Exception as e:
                            print(f"  [refresh] Error: {e}")

                refresh_task = asyncio.create_task(refresh_loop())

                # Main receive loop
                try:
                    async for raw in ws:
                        if not _state["running"]:
                            break
                        _handle_message(raw)
                        _print_status()
                except websockets.ConnectionClosed as e:
                    print(f"\n  Connection closed: {e}")
                    _write_event("connection_closed", {"reason": str(e)})
                finally:
                    ping_task.cancel()
                    refresh_task.cancel()

        except Exception as e:
            retry_count += 1
            wait = min(2 ** retry_count, 60)
            print(f"  Connection error ({retry_count}/{max_retries}): {e}")
            print(f"  Reconnecting in {wait}s...")
            _write_event("connection_error", {"error": str(e), "retry": retry_count})
            await asyncio.sleep(wait)

    # Cleanup
    _state["log_file"].flush()
    _state["log_file"].close()

    elapsed = time.time() - _state["connection_started"]
    summary = (
        f"WS Logger stopped after {elapsed/60:.0f}min: "
        f"{_state['events_logged']} events, "
        f"{_state['resolutions_seen']} resolutions, "
        f"{_state['price_changes_seen']} price changes, "
        f"{_state['last_trade_prices_seen']} trades"
    )
    print(f"\n  {summary}")
    print(f"  Log: {log_path}")
    _discord_notify(summary)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Log Polymarket WebSocket events (resolution diagnostic)"
    )
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS_DEFAULT,
                        help=f"Max token IDs to subscribe (default: {MAX_TOKENS_DEFAULT}, max: 500)")
    args = parser.parse_args()

    # Load .env for Discord webhook
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    def shutdown(sig, frame):
        print("\n  Shutting down...")
        _state["running"] = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        asyncio.run(run_logger(args.max_tokens))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
