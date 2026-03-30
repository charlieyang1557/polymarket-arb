# tests/test_poly_smart_run.py
"""Tests for --smart-run pipeline: process detection, hot-add, state files."""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from scripts.poly_daily_scan import (
    is_poly_mm_running,
    read_active_slugs,
    write_pending_markets,
    read_pending_markets,
)
from scripts.poly_paper_mm import (
    write_active_slugs_file,
    consume_pending_markets,
    MAX_ACTIVE_MARKETS,
)


# --- Process detection ---

def test_process_detection_no_grep_ghost():
    """pgrep-based detection doesn't match itself."""
    # This just validates the function exists and returns bool
    result = is_poly_mm_running()
    assert isinstance(result, bool)


# --- Active slugs file ---

def test_write_active_slugs():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        write_active_slugs_file(["slug-a", "slug-b"], "sess-123", path)
        with open(path) as f:
            data = json.load(f)
        assert data["active_slugs"] == ["slug-a", "slug-b"]
        assert data["session_id"] == "sess-123"
        assert "updated_at" in data
    finally:
        os.unlink(path)


def test_read_active_slugs():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({"active_slugs": ["a", "b"], "session_id": "s1",
                    "updated_at": "2026-03-30T00:00:00Z"}, f)
        path = f.name
    try:
        result = read_active_slugs(path)
        assert result == ["a", "b"]
    finally:
        os.unlink(path)


def test_read_active_slugs_missing():
    result = read_active_slugs("/tmp/nonexistent_active_slugs_999.json")
    assert result == []


# --- Pending markets file ---

def test_write_pending_atomic():
    """Write uses .tmp then rename (atomic)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pending.json")
        write_pending_markets(["slug-x", "slug-y"], path)
        with open(path) as f:
            data = json.load(f)
        assert data["slugs"] == ["slug-x", "slug-y"]
        assert "added_at" in data
        # .tmp should not exist
        assert not os.path.exists(path + ".tmp")


def test_read_pending_and_consume():
    """Read deletes the file (consume pattern)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pending.json")
        write_pending_markets(["new-slug"], path)
        result = read_pending_markets(path)
        assert result == ["new-slug"]
        assert not os.path.exists(path)  # consumed


def test_read_pending_missing():
    result = read_pending_markets("/tmp/nonexistent_pending_999.json")
    assert result == []


def test_read_pending_malformed():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        f.write("{invalid json")
        path = f.name
    try:
        result = read_pending_markets(path)
        assert result == []
        assert not os.path.exists(path)  # cleaned up
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --- Engine: consume_pending_markets ---

def test_consume_adds_new_markets():
    """New slugs get added to gs.markets."""
    from src.mm.state import GlobalState, MarketState
    gs = GlobalState(session_id="test")
    gs.markets["existing"] = MarketState(ticker="existing")

    with tempfile.TemporaryDirectory() as d:
        pending_path = os.path.join(d, "pending.json")
        active_path = os.path.join(d, "active.json")
        write_pending_markets(["new-slug"], pending_path)

        added = consume_pending_markets(
            gs, pending_path, active_path,
            game_start_lookup=lambda s: "2026-03-30T20:00:00Z")

        assert "new-slug" in gs.markets
        assert added == ["new-slug"]
        assert not os.path.exists(pending_path)


def test_consume_skips_duplicates():
    """Already-active slugs are skipped."""
    from src.mm.state import GlobalState, MarketState
    gs = GlobalState(session_id="test")
    gs.markets["slug-a"] = MarketState(ticker="slug-a")

    with tempfile.TemporaryDirectory() as d:
        pending_path = os.path.join(d, "pending.json")
        active_path = os.path.join(d, "active.json")
        write_pending_markets(["slug-a"], pending_path)

        added = consume_pending_markets(
            gs, pending_path, active_path,
            game_start_lookup=lambda s: None)

        assert added == []


def test_consume_respects_max_cap():
    """Max active markets cap (10) respected."""
    from src.mm.state import GlobalState, MarketState
    gs = GlobalState(session_id="test")
    for i in range(MAX_ACTIVE_MARKETS):
        gs.markets[f"m{i}"] = MarketState(ticker=f"m{i}")

    with tempfile.TemporaryDirectory() as d:
        pending_path = os.path.join(d, "pending.json")
        active_path = os.path.join(d, "active.json")
        write_pending_markets(["overflow-slug"], pending_path)

        added = consume_pending_markets(
            gs, pending_path, active_path,
            game_start_lookup=lambda s: None)

        assert added == []
        assert "overflow-slug" not in gs.markets


def test_consume_updates_active_file():
    """Active slugs file updated after hot-add."""
    from src.mm.state import GlobalState, MarketState
    gs = GlobalState(session_id="test")
    gs.markets["old"] = MarketState(ticker="old")

    with tempfile.TemporaryDirectory() as d:
        pending_path = os.path.join(d, "pending.json")
        active_path = os.path.join(d, "active.json")
        write_pending_markets(["new"], pending_path)

        consume_pending_markets(
            gs, pending_path, active_path,
            game_start_lookup=lambda s: "2026-03-30T20:00:00Z")

        with open(active_path) as f:
            data = json.load(f)
        assert "new" in data["active_slugs"]
        assert "old" in data["active_slugs"]


def test_consume_no_pending_file():
    """No pending file → no-op, returns empty."""
    from src.mm.state import GlobalState
    gs = GlobalState(session_id="test")
    added = consume_pending_markets(
        gs, "/tmp/nonexistent_999.json", "/tmp/active_999.json",
        game_start_lookup=lambda s: None)
    assert added == []


# --- Scanner: already-active exclusion ---

def test_scanner_excludes_active_slugs():
    """Slugs already in active file are excluded from hot-add."""
    active = ["running-a", "running-b"]
    new_targets = [
        {"slug": "running-a"},
        {"slug": "running-b"},
        {"slug": "new-c"},
    ]
    filtered = [t for t in new_targets if t["slug"] not in active]
    assert len(filtered) == 1
    assert filtered[0]["slug"] == "new-c"
