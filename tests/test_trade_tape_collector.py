"""Tests for the WebSocket trade tape collector.

The collector subscribes to Polymarket US `SUBSCRIPTION_TYPE_TRADE` for the
current active slugs and persists each Trade message (with maker/taker
side + intent) to a dedicated DB table `mm_trade_tape`.

These tests cover pure-function components:
  - Trade message parsing (SDK Trade dict → row dict, with price/qty conversion)
  - DB schema initialization (idempotent CREATE)
  - Row persistence
  - Slug-set diff (when poly_active_slugs.json changes mid-session)

Live WebSocket integration is not tested here — see the integration check
in [scripts/trade_tape_collector.py](scripts/trade_tape_collector.py)
which connects briefly and verifies rows persist.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from scripts.trade_tape_collector import (
    init_db,
    parse_trade_message,
    persist_trade,
    compute_slug_diff,
    read_active_slugs,
    taker_yes_or_no,
    taker_buy_or_sell,
)


# ---- Trade message parsing -----------------------------------------------

SAMPLE_TRADE_MSG = {
    # Real Trade message format confirmed against live polymarket_us
    # WebSocket on 2026-05-11. side/intent are fully-qualified enum strings:
    #   side: ORDER_SIDE_BUY | ORDER_SIDE_SELL
    #   intent: ORDER_INTENT_BUY_LONG (buy YES) | ORDER_INTENT_BUY_SHORT (buy NO)
    #         | ORDER_INTENT_SELL_LONG (sell YES) | ORDER_INTENT_SELL_SHORT (sell NO)
    #         | ORDER_INTENT_UNDEFINED (often seen on the maker side)
    # The combination (taker.side, taker.intent) tells us aggressor direction;
    # e.g., taker.side=SELL + taker.intent=BUY_SHORT means the taker sold YES
    # (= effectively bought NO).
    "requestId": "trade-sub-1",
    "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
    "trade": {
        "marketSlug": "tsc-mlb-az-tex-2026-05-11-7pt5",
        "price": {"value": "0.48", "currency": "USD"},
        "quantity": {"value": "5", "currency": "USD"},
        "tradeTime": "2026-05-11T18:30:15.123456+00:00",
        "maker": {"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_UNDEFINED"},
        "taker": {"side": "ORDER_SIDE_SELL", "intent": "ORDER_INTENT_BUY_SHORT"},
    },
}


def test_parse_trade_message_extracts_core_fields():
    """Trade message → row dict with cents conversion."""
    row = parse_trade_message(SAMPLE_TRADE_MSG)
    assert row["market_slug"] == "tsc-mlb-az-tex-2026-05-11-7pt5"
    assert row["price_cents"] == 48
    assert row["quantity"] == 5
    assert row["trade_time"] == "2026-05-11T18:30:15.123456+00:00"
    assert row["maker_side"] == "ORDER_SIDE_BUY"
    assert row["maker_intent"] == "ORDER_INTENT_UNDEFINED"
    assert row["taker_side"] == "ORDER_SIDE_SELL"
    assert row["taker_intent"] == "ORDER_INTENT_BUY_SHORT"
    # raw_json captures the original payload for debugging
    assert row["raw_json"] is not None
    decoded = json.loads(row["raw_json"])
    assert decoded["trade"]["marketSlug"] == SAMPLE_TRADE_MSG["trade"]["marketSlug"]


def test_parse_trade_message_handles_fractional_price():
    """Sub-cent prices are rounded (Polymarket prices are integer cents)."""
    msg = {**SAMPLE_TRADE_MSG, "trade": {**SAMPLE_TRADE_MSG["trade"]}}
    msg["trade"]["price"] = {"value": "0.4951", "currency": "USD"}
    row = parse_trade_message(msg)
    assert row["price_cents"] == 50  # rounds to nearest


def test_parse_trade_message_extreme_prices():
    """1c and 99c boundary prices parse correctly."""
    for dollar, expected_cents in [("0.01", 1), ("0.99", 99), ("0.50", 50)]:
        msg = {**SAMPLE_TRADE_MSG, "trade": {**SAMPLE_TRADE_MSG["trade"]}}
        msg["trade"]["price"] = {"value": dollar, "currency": "USD"}
        row = parse_trade_message(msg)
        assert row["price_cents"] == expected_cents, f"{dollar} → {row['price_cents']}"


def test_parse_trade_message_aggressor_decode():
    """Confirms fields preserve the aggressor-side info for the YES-seller
    taker case (which is the case fill_asymmetry_diagnosis.md flagged
    as the structural source of our yes_bid over-fill)."""
    # taker.side=SELL, taker.intent=BUY_SHORT → taker sold YES (= bought NO)
    # i.e., this trade was a YES-seller taker hitting our YES_BID maker.
    msg = {**SAMPLE_TRADE_MSG, "trade": {**SAMPLE_TRADE_MSG["trade"]}}
    msg["trade"]["maker"] = {"side": "ORDER_SIDE_BUY",
                              "intent": "ORDER_INTENT_BUY_LONG"}
    msg["trade"]["taker"] = {"side": "ORDER_SIDE_SELL",
                              "intent": "ORDER_INTENT_BUY_SHORT"}
    row = parse_trade_message(msg)
    assert row["maker_side"] == "ORDER_SIDE_BUY"
    assert row["maker_intent"] == "ORDER_INTENT_BUY_LONG"
    assert row["taker_side"] == "ORDER_SIDE_SELL"
    assert row["taker_intent"] == "ORDER_INTENT_BUY_SHORT"


def test_parse_trade_message_quantity_is_integer():
    """SDK sends quantities as strings; we want ints."""
    msg = {**SAMPLE_TRADE_MSG, "trade": {**SAMPLE_TRADE_MSG["trade"]}}
    msg["trade"]["quantity"] = {"value": "42", "currency": "USD"}
    row = parse_trade_message(msg)
    assert row["quantity"] == 42
    assert isinstance(row["quantity"], int)


def test_parse_trade_message_missing_intent_defaults_to_empty():
    """SDK may emit intent=None or missing field; parser should not crash."""
    msg = {**SAMPLE_TRADE_MSG, "trade": {**SAMPLE_TRADE_MSG["trade"]}}
    msg["trade"]["maker"] = {"side": "ORDER_SIDE_BUY"}  # no intent
    row = parse_trade_message(msg)
    assert row["maker_side"] == "ORDER_SIDE_BUY"
    assert row["maker_intent"] == ""  # defaulted, not None


def test_parse_trade_message_rejects_non_trade_messages():
    """Non-Trade messages (heartbeat, error, etc.) raise ValueError."""
    with pytest.raises(ValueError):
        parse_trade_message({"heartbeat": {}})
    with pytest.raises(ValueError):
        parse_trade_message({"requestId": "x", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA"})


# ---- DB schema initialization --------------------------------------------

def test_init_db_creates_table_and_index():
    """init_db creates mm_trade_tape table + index."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = sqlite3.connect(path)
        init_db(conn)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mm_trade_tape'"
        )
        assert cursor.fetchone() is not None
        # index exists
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_trade_tape_slug_time'"
        ).fetchone()
        assert idx is not None
        conn.close()
    finally:
        os.unlink(path)


def test_init_db_is_idempotent():
    """Running init_db twice does not error."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = sqlite3.connect(path)
        init_db(conn)
        init_db(conn)  # second call should be fine
        conn.close()
    finally:
        os.unlink(path)


# ---- Persistence ---------------------------------------------------------

def test_persist_trade_round_trips():
    """Insert a trade row, query it back, fields match."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = sqlite3.connect(path)
        init_db(conn)
        row = parse_trade_message(SAMPLE_TRADE_MSG)
        new_id = persist_trade(conn, row)
        assert new_id is not None and new_id > 0
        cur = conn.execute(
            "SELECT market_slug, price_cents, quantity, maker_side, maker_intent, "
            "taker_side, taker_intent, trade_time FROM mm_trade_tape WHERE id = ?",
            (new_id,),
        )
        fetched = cur.fetchone()
        assert fetched == (
            "tsc-mlb-az-tex-2026-05-11-7pt5", 48, 5,
            "ORDER_SIDE_BUY", "ORDER_INTENT_UNDEFINED",
            "ORDER_SIDE_SELL", "ORDER_INTENT_BUY_SHORT",
            "2026-05-11T18:30:15.123456+00:00",
        )
        conn.close()
    finally:
        os.unlink(path)


def test_persist_trade_records_recorded_at():
    """recorded_at defaults to now and is non-null."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn = sqlite3.connect(path)
        init_db(conn)
        row = parse_trade_message(SAMPLE_TRADE_MSG)
        persist_trade(conn, row)
        recorded = conn.execute("SELECT recorded_at FROM mm_trade_tape").fetchone()[0]
        assert recorded is not None
        assert len(recorded) > 0
        conn.close()
    finally:
        os.unlink(path)


# ---- Slug-set diff -------------------------------------------------------

def test_compute_slug_diff_finds_additions():
    """If active slugs gain a new entry, diff returns (to_add, to_remove)."""
    current = {"tsc-a", "tsc-b"}
    target = {"tsc-a", "tsc-b", "tsc-c"}
    to_add, to_remove = compute_slug_diff(current, target)
    assert to_add == {"tsc-c"}
    assert to_remove == set()


def test_compute_slug_diff_finds_removals():
    current = {"tsc-a", "tsc-b", "tsc-c"}
    target = {"tsc-a"}
    to_add, to_remove = compute_slug_diff(current, target)
    assert to_add == set()
    assert to_remove == {"tsc-b", "tsc-c"}


def test_compute_slug_diff_mixed():
    current = {"tsc-a", "tsc-b"}
    target = {"tsc-b", "tsc-c"}
    to_add, to_remove = compute_slug_diff(current, target)
    assert to_add == {"tsc-c"}
    assert to_remove == {"tsc-a"}


def test_compute_slug_diff_no_change():
    current = {"tsc-a", "tsc-b"}
    target = {"tsc-a", "tsc-b"}
    to_add, to_remove = compute_slug_diff(current, target)
    assert to_add == set()
    assert to_remove == set()


# ---- Aggressor-side enum decode -----------------------------------------

def test_taker_yes_or_no_long_means_yes():
    """ORDER_INTENT_*_LONG → 'yes' (YES side)."""
    assert taker_yes_or_no("ORDER_SIDE_BUY", "ORDER_INTENT_BUY_LONG") == "yes"
    assert taker_yes_or_no("ORDER_SIDE_SELL", "ORDER_INTENT_SELL_LONG") == "yes"


def test_taker_yes_or_no_short_means_no():
    """ORDER_INTENT_*_SHORT → 'no' (NO side)."""
    assert taker_yes_or_no("ORDER_SIDE_SELL", "ORDER_INTENT_BUY_SHORT") == "no"
    assert taker_yes_or_no("ORDER_SIDE_BUY", "ORDER_INTENT_SELL_SHORT") == "no"


def test_taker_yes_or_no_undefined_returns_empty():
    """ORDER_INTENT_UNDEFINED (rare on taker side) → empty string."""
    assert taker_yes_or_no("ORDER_SIDE_BUY", "ORDER_INTENT_UNDEFINED") == ""
    assert taker_yes_or_no("", "") == ""


def test_taker_buy_or_sell():
    """ORDER_SIDE_BUY → 'buy', ORDER_SIDE_SELL → 'sell'."""
    assert taker_buy_or_sell("ORDER_SIDE_BUY") == "buy"
    assert taker_buy_or_sell("ORDER_SIDE_SELL") == "sell"
    assert taker_buy_or_sell("") == ""


def test_aggressor_round_trip_for_yes_seller_taker():
    """A real-data sample: taker.side=SELL + intent=BUY_SHORT means taker
    SOLD YES (= bought NO). For drain_queue analysis: this trade hits the
    YES_BID side of the book. We want to count it for our yes_bid maker
    orders, not for our no_bid maker orders."""
    assert taker_yes_or_no("ORDER_SIDE_SELL", "ORDER_INTENT_BUY_SHORT") == "no"
    assert taker_buy_or_sell("ORDER_SIDE_SELL") == "sell"
    # Together: "taker sold (= hit bid), in the NO direction (i.e., NO side
    # of the YES/NO contract pair was the aggressor's destination)".
    # For our YES_BID maker: this trade DOES drain our queue if we had a
    # YES bid at this price (because the seller hit the YES bid book to
    # establish a short).


# ---- Active slugs file parsing -------------------------------------------

def test_read_active_slugs_reads_set_from_json():
    """read_active_slugs returns a set of slugs from poly_active_slugs.json."""
    payload = {
        "session_id": "test",
        "active_slugs": ["tsc-a", "tsc-b"],
        "updated_at": "2026-05-11T00:00:00+00:00",
    }
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump(payload, f)
        path = f.name
    try:
        slugs = read_active_slugs(path)
        assert slugs == {"tsc-a", "tsc-b"}
    finally:
        os.unlink(path)


def test_read_active_slugs_missing_file_returns_empty_set():
    """If poly_active_slugs.json doesn't exist (e.g., bot not running), return empty set."""
    slugs = read_active_slugs("/tmp/nonexistent_active_slugs.json")
    assert slugs == set()


def test_read_active_slugs_malformed_file_returns_empty():
    """Malformed JSON should not crash the collector."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        f.write("not-json{")
        path = f.name
    try:
        slugs = read_active_slugs(path)
        assert slugs == set()
    finally:
        os.unlink(path)
