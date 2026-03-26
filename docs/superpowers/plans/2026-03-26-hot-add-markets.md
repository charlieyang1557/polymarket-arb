# Hot-Add Markets to Running Session — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the afternoon scanner to inject newly discovered markets into a running bot session, instead of wasting them until the next restart.

**Architecture:** Scanner writes a `pending_markets.json` file (atomic rename). Engine checks for it every 60s (6th tick), creates fresh MarketState for each new ticker, adds to rotation. 15 active market cap. Schedule staleness revokes e-sports allowance after 6h.

**Tech Stack:** Python, SQLite, JSON, os.rename for atomic writes.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/kalshi_daily_scan.py` | Modify (lines 524-531) | Write pending_markets.json when bot running |
| `src/mm/engine.py` | Modify (add method) | Check + load pending markets every 60s |
| `scripts/paper_mm.py` | Modify (line 144) | Pass pending check into tick loop |
| `tests/test_mm_engine.py` | Modify | Engine hot-add tests |
| `tests/test_daily_scan.py` | Modify | Scanner pending file tests |

---

### Task 1: Scanner writes pending_markets.json (atomic)

**Files:**
- Modify: `scripts/kalshi_daily_scan.py:524-531`
- Modify: `tests/test_daily_scan.py`

**Context:** When `--smart-run` detects bot already running (line 527), it currently just prints "saved for next session". Instead, write a `pending_markets.json` with the new targets for the engine to pick up.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daily_scan.py

def test_smart_run_writes_pending_markets(tmp_path, monkeypatch):
    """--smart-run with running bot writes pending_markets.json atomically."""
    from scripts.kalshi_daily_scan import write_pending_markets
    targets = [
        {"ticker": "KXNBASPREAD-T1", "title": "NBA Game 1",
         "game_start_utc": "2026-03-26T23:00:00Z"},
        {"ticker": "KXNHLSPREAD-T2", "title": "NHL Game 2",
         "game_start_utc": None},
    ]
    out_path = tmp_path / "pending_markets.json"
    write_pending_markets(targets, str(out_path))
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert len(data) == 2
    assert data[0]["ticker"] == "KXNBASPREAD-T1"
    # .tmp file should not linger
    assert not (tmp_path / "pending_markets.json.tmp").exists()


def test_smart_run_excludes_active_session_tickers(tmp_path):
    """Tickers already in the running session are excluded from pending."""
    from scripts.kalshi_daily_scan import write_pending_markets
    targets = [
        {"ticker": "KXNBASPREAD-T1", "title": "Game 1"},
        {"ticker": "KXNBASPREAD-T2", "title": "Game 2"},
    ]
    active_tickers = {"KXNBASPREAD-T1"}
    write_pending_markets(targets, str(tmp_path / "pending.json"),
                          active_tickers=active_tickers)
    data = json.loads((tmp_path / "pending.json").read_text())
    assert len(data) == 1
    assert data[0]["ticker"] == "KXNBASPREAD-T2"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python -m pytest tests/test_daily_scan.py::test_smart_run_writes_pending_markets tests/test_daily_scan.py::test_smart_run_excludes_active_session_tickers -v`
Expected: ImportError — `write_pending_markets` not defined

- [ ] **Step 3: Implement write_pending_markets()**

In `scripts/kalshi_daily_scan.py`, add near line 100 (after imports):

```python
PENDING_MARKETS_PATH = "data/pending_markets.json"


def write_pending_markets(targets: list[dict], path: str = PENDING_MARKETS_PATH,
                          active_tickers: set[str] | None = None) -> int:
    """Atomically write pending markets for engine hot-add.

    Returns count of markets written (after filtering active tickers).
    """
    active_tickers = active_tickers or set()
    new_targets = [t for t in targets if t["ticker"] not in active_tickers]
    if not new_targets:
        return 0

    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(new_targets, f, indent=2)
    os.rename(tmp_path, path)
    return len(new_targets)
```

Then update the `--smart-run` block (lines 527-531):

```python
        if bot_running and args.smart_run:
            print(f"\n  Bot already running (PID={bot_pid}). Skipping launch.")
            # Write pending file for engine hot-add
            n_queued = write_pending_markets(targets)
            if n_queued:
                queued_tickers = [t["ticker"] for t in targets
                                 if t["ticker"] not in existing_tickers][:n_queued]
                print(f"  Queued {n_queued} new markets for hot-add.")
                lines.append(f"Queued {n_queued} new markets for hot-add: "
                             f"{', '.join(queued_tickers)}")
            else:
                print("  No new markets to queue.")
                lines.append("Afternoon scan: no new markets to add.")
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `python -m pytest tests/test_daily_scan.py -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add scripts/kalshi_daily_scan.py tests/test_daily_scan.py
git commit -m "feat(scanner): write pending_markets.json for hot-add (atomic)"
```

---

### Task 2: Engine checks and loads pending markets

**Files:**
- Modify: `src/mm/engine.py` (add `check_pending_markets` method)
- Modify: `tests/test_mm_engine.py`

**Context:** Engine's tick loop runs every 10s. Every 6th tick (~60s, line 437), it already does snapshots. Add pending market check at the same cadence.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mm_engine.py
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from src.mm.state import MarketState, GlobalState


def test_engine_loads_pending_markets(tmp_path):
    """Engine picks up pending_markets.json and creates MarketState."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    gs.markets["EXISTING"] = MarketState(ticker="EXISTING")

    pending = [
        {"ticker": "NEW1", "game_start_utc": "2026-03-26T23:00:00Z"},
        {"ticker": "NEW2"},
    ]
    pending_path = str(tmp_path / "pending_markets.json")
    with open(pending_path, "w") as f:
        json.dump(pending, f)

    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == ["NEW1", "NEW2"]
    assert "NEW1" in gs.markets
    assert "NEW2" in gs.markets
    assert gs.markets["NEW1"].game_start_utc is not None
    assert gs.markets["NEW2"].game_start_utc is None
    # File should be deleted after processing
    assert not os.path.exists(pending_path)


def test_engine_skips_duplicate_tickers(tmp_path):
    """Tickers already in gs.markets are skipped."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    gs.markets["DUP"] = MarketState(ticker="DUP")

    pending_path = str(tmp_path / "pending_markets.json")
    with open(pending_path, "w") as f:
        json.dump([{"ticker": "DUP"}, {"ticker": "FRESH"}], f)

    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == ["FRESH"]
    assert len(gs.markets) == 2  # DUP + FRESH


def test_engine_respects_active_market_cap(tmp_path):
    """Cap counts only active markets; exited markets don't count."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    # 10 exited + 4 active = 14 total, 4 active
    for i in range(10):
        ms = MarketState(ticker=f"EXIT{i}")
        ms.active = False
        gs.markets[f"EXIT{i}"] = ms
    for i in range(4):
        gs.markets[f"ACTIVE{i}"] = MarketState(ticker=f"ACTIVE{i}")

    pending_path = str(tmp_path / "pending_markets.json")
    # Try to add 12 new markets with cap=15
    new = [{"ticker": f"NEW{i}"} for i in range(12)]
    with open(pending_path, "w") as f:
        json.dump(new, f)

    added = load_pending_markets(gs, pending_path, max_active=15)
    # 4 active + 11 new = 15 (cap). Only 11 should be added.
    assert len(added) == 11
    active_count = sum(1 for m in gs.markets.values() if m.active)
    assert active_count == 15


def test_engine_handles_malformed_pending(tmp_path):
    """Malformed JSON doesn't crash — logs warning, skips."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    pending_path = str(tmp_path / "pending_markets.json")
    with open(pending_path, "w") as f:
        f.write("{invalid json")

    added = load_pending_markets(gs, pending_path, max_active=15)
    assert added == []
    # File should be deleted even if malformed (prevent retry loop)
    assert not os.path.exists(pending_path)


def test_engine_no_pending_file(tmp_path):
    """No pending file → empty list, no error."""
    from src.mm.engine import load_pending_markets

    gs = GlobalState(session_id="test")
    added = load_pending_markets(gs, str(tmp_path / "nope.json"), max_active=15)
    assert added == []
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python -m pytest tests/test_mm_engine.py::test_engine_loads_pending_markets tests/test_mm_engine.py::test_engine_skips_duplicate_tickers tests/test_mm_engine.py::test_engine_respects_active_market_cap tests/test_mm_engine.py::test_engine_handles_malformed_pending tests/test_mm_engine.py::test_engine_no_pending_file -v`
Expected: ImportError — `load_pending_markets` not defined

- [ ] **Step 3: Implement load_pending_markets()**

In `src/mm/engine.py`, add as a module-level function (near top, after imports):

```python
PENDING_MARKETS_PATH = "data/pending_markets.json"
MAX_ACTIVE_MARKETS = 15


def load_pending_markets(gs: GlobalState, path: str = PENDING_MARKETS_PATH,
                         max_active: int = MAX_ACTIVE_MARKETS) -> list[str]:
    """Check for pending_markets.json, add new markets to session.

    Returns list of ticker strings that were added.
    """
    if not os.path.exists(path):
        return []

    try:
        with open(path) as f:
            pending = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Malformed pending_markets.json: %s", e)
        os.remove(path)
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

    os.remove(path)
    return added
```

Add `import json` to engine.py imports if not already present.

- [ ] **Step 4: Run tests — verify they pass**

Run: `python -m pytest tests/test_mm_engine.py -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/mm/engine.py tests/test_mm_engine.py
git commit -m "feat(engine): load_pending_markets for hot-add with active cap"
```

---

### Task 3: Wire hot-add into paper_mm.py tick loop

**Files:**
- Modify: `scripts/paper_mm.py:127-151` (main loop)

**Context:** The main loop in paper_mm.py cycles through active tickers. Every 6th cycle (matching the snapshot cadence), call `load_pending_markets()` and add new tickers to the rotation.

- [ ] **Step 1: Implement hot-add check in main loop**

In `scripts/paper_mm.py`, after the cycle increment (line 151), add:

```python
            # Hot-add pending markets (every 6th cycle, ~60s)
            if cycle % 6 == 0:
                new_tickers = load_pending_markets(gs)
                if new_tickers:
                    tickers.extend(new_tickers)
                    for t in new_tickers:
                        print(f"  HOT-ADD [{t}] added to session")
                    discord_notify(
                        f"**Hot-Add** {len(new_tickers)} markets added: "
                        f"{', '.join(new_tickers)}")
```

Also add import at top of paper_mm.py:

```python
from src.mm.engine import load_pending_markets
```

- [ ] **Step 2: Verify manually (no unit test needed — integration concern)**

The tick loop is not unit-tested (it's the integration glue). The components it calls (`load_pending_markets`, `tick_one_market`) are individually tested.

- [ ] **Step 3: Commit**

```bash
git add scripts/paper_mm.py
git commit -m "feat(paper_mm): wire hot-add into tick loop (every 60s)"
```

---

### Task 4: Schedule staleness check

**Files:**
- Modify: `scripts/kalshi_daily_scan.py` (load_game_schedule)
- Modify: `tests/test_daily_scan.py`

**Context:** If game_schedule.json hasn't been updated in >6h, e-sports allowance should be revoked (stale schedule data is dangerous — wrong game_start_utc means L4 won't exit properly).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daily_scan.py

def test_schedule_staleness_revokes_esports(tmp_path):
    """Schedule >6h old → e-sports tickers get no game_start_utc."""
    schedule_file = tmp_path / "game_schedule.json"
    old_time = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": old_time,
        "games": [{
            "sport": "LOL",
            "start_time_utc": "2026-03-26T23:00:00Z",
            "kalshi_markets": ["KXLOLTOTALMAPS-T1"]
        }]
    }))
    schedule = load_game_schedule(str(schedule_file))
    # Stale schedule → empty (e-sports data not trustworthy)
    assert schedule == {}


def test_schedule_fresh_allows_esports(tmp_path):
    """Schedule <6h old → e-sports tickers get game_start_utc."""
    schedule_file = tmp_path / "game_schedule.json"
    fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    schedule_file.write_text(json.dumps({
        "updated_at": fresh_time,
        "games": [{
            "sport": "LOL",
            "start_time_utc": "2026-03-26T23:00:00Z",
            "kalshi_markets": ["KXLOLTOTALMAPS-T1"]
        }]
    }))
    schedule = load_game_schedule(str(schedule_file))
    assert "KXLOLTOTALMAPS-T1" in schedule


def test_schedule_no_updated_at_treated_as_stale(tmp_path):
    """Missing updated_at → treat as stale, return empty."""
    schedule_file = tmp_path / "game_schedule.json"
    schedule_file.write_text(json.dumps({
        "games": [{
            "sport": "NBA",
            "start_time_utc": "2026-03-26T23:00:00Z",
            "kalshi_markets": ["KXNBASPREAD-T1"]
        }]
    }))
    schedule = load_game_schedule(str(schedule_file))
    # No updated_at → stale → empty
    assert schedule == {}
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python -m pytest tests/test_daily_scan.py::test_schedule_staleness_revokes_esports tests/test_daily_scan.py::test_schedule_fresh_allows_esports tests/test_daily_scan.py::test_schedule_no_updated_at_treated_as_stale -v`
Expected: FAIL

- [ ] **Step 3: Implement staleness check in load_game_schedule()**

Replace `load_game_schedule()` in `scripts/kalshi_daily_scan.py`:

```python
SCHEDULE_MAX_AGE_HOURS = 6


def load_game_schedule(path: str = SCHEDULE_PATH) -> dict[str, str]:
    """Load game schedule and build ticker -> start_time_utc lookup.

    Returns empty dict if file is missing, malformed, or stale (>6h old).
    Staleness check prevents acting on outdated game_start_utc values,
    which would break L4 time-based exit for e-sports.
    """
    schedule = {}
    try:
        with open(path) as f:
            data = json.load(f)

        # Staleness check
        updated_at = data.get("updated_at")
        if not updated_at:
            print(f"  WARNING: game_schedule.json missing updated_at — treating as stale")
            return {}
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
        if age_hours > SCHEDULE_MAX_AGE_HOURS:
            print(f"  WARNING: game_schedule.json is {age_hours:.1f}h old — treating as stale")
            return {}

        for game in data.get("games", []):
            start = game.get("start_time_utc", "")
            for ticker in (game.get("kalshi_markets") or []):
                schedule[ticker] = start
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return schedule
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `python -m pytest tests/test_daily_scan.py -q`
Expected: All pass

**Note:** Existing tests that use `load_game_schedule` with test fixtures that lack `updated_at` will now return `{}`. Check and fix any broken tests by adding `"updated_at"` to their fixtures.

- [ ] **Step 5: Commit**

```bash
git add scripts/kalshi_daily_scan.py tests/test_daily_scan.py
git commit -m "feat(scanner): schedule staleness check — revoke e-sports after 6h"
```

---

### Task 5: Run full test suite and push

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/test_mm_*.py tests/test_*skew*.py tests/test_*spread*.py tests/test_*obi*.py tests/test_pregame*.py tests/test_silent*.py tests/test_monitor*.py tests/test_inventory*.py tests/test_daily_scan.py tests/test_session_summary.py tests/test_verify_pnl.py -q
```

Expected: All pass

- [ ] **Step 2: Squash commit and push**

```bash
git push origin main
```

---

## Safety Checklist

- [ ] 15 active market cap enforced (exited markets don't count)
- [ ] New MarketState starts fresh (inv=0, pnl=0)
- [ ] All L1-L4 risk checks apply to hot-added markets (they go through same tick_one_market)
- [ ] Malformed pending file → log + skip, no crash
- [ ] Atomic write (tmp + rename) prevents half-read
- [ ] File deleted after processing (no re-add loop)
- [ ] Schedule staleness revokes e-sports allowance
