"""
tests/test_state_pruning.py — records for targets that no longer exist.

Found by running the thing, not by reading it
----------------------------------------------
During a live dead-man's-switch test the dashboard read "estos datos tienen 978
min" while the monitor had probed sixty seconds earlier. Two records in
state.json belonged to targets removed from the config that morning, and
nothing had ever deleted them.

They were not inert:

  - `stale_seconds()` answers with the age of the OLDEST record, so one
    abandoned entry pinned the freshness banner to "dead" permanently. An
    indicator that is always red is an indicator nobody reads — the exact
    failure the banner exists to prevent, happening to the banner.
  - The page rendered a row per record, so it showed six targets while the
    editor below it listed four. The same viewport contradicted itself.

The dashboard editor turned "remove a target" into one click, which moved this
from a rare state to the expected one.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from api import stale_seconds
from main import StateManager

KEPT    = "https://kept.example"
REMOVED = "https://removed.example"


async def _state_with(tmp_path: Path) -> StateManager:
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(KEPT)
    await sm.set_up(REMOVED)
    return sm


# =============================================================================
# A. Pruning itself
# =============================================================================


async def test_a_target_no_longer_configured_is_dropped(tmp_path: Path) -> None:
    sm = await _state_with(tmp_path)
    assert await sm.prune([KEPT]) == [REMOVED]
    assert set(sm._state) == {KEPT}


async def test_configured_targets_are_never_touched(tmp_path: Path) -> None:
    """The record carries alert suppression, strike counts and reminder state.
    Dropping a live target's record would re-fire every alert it is currently
    suppressing."""
    sm = await _state_with(tmp_path)
    before = dict(sm._state[KEPT])
    await sm.prune([KEPT, REMOVED])
    assert sm._state[KEPT] == before


async def test_pruning_nothing_writes_nothing(tmp_path: Path) -> None:
    """The common case is "no change", and it runs every 60 seconds forever.
    Rewriting the file each time would burn disk for no reason."""
    sm = await _state_with(tmp_path)
    state_file = tmp_path / "state.json"
    mtime = state_file.stat().st_mtime_ns
    assert await sm.prune([KEPT, REMOVED]) == []
    assert state_file.stat().st_mtime_ns == mtime


async def test_the_removal_is_persisted(tmp_path: Path) -> None:
    sm = await _state_with(tmp_path)
    await sm.prune([KEPT])
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert set(stored["targets"]) == {KEPT}


async def test_an_empty_target_list_clears_everything(tmp_path: Path) -> None:
    """"The operator deleted every target" is a real state the dashboard
    editor can produce, and it must not be confused with "no list supplied"."""
    sm = await _state_with(tmp_path)
    assert set(await sm.prune([])) == {KEPT, REMOVED}
    assert sm._state == {}


# =============================================================================
# B. The consequence that made it matter
# =============================================================================


async def test_an_abandoned_record_no_longer_pins_freshness_to_dead(tmp_path) -> None:
    """The bug, reproduced and then closed.

    stale_seconds() takes the OLDEST record. With one abandoned entry from
    sixteen hours ago beside four probed a minute ago, the banner read "dead"
    while the monitor was perfectly healthy -- and kept reading it, so killing
    the monitor for real changed nothing on screen.
    """
    sm   = await _state_with(tmp_path)
    now  = datetime.now(timezone.utc)
    sm._state[KEPT]["last_checked"]    = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sm._state[REMOVED]["last_checked"] = (
        now - timedelta(hours=16)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    before = stale_seconds(sm._state)
    assert before > 15 * 3600, "the abandoned record dominates: banner reads dead"

    await sm.prune([KEPT])
    after = stale_seconds(sm._state)
    assert after < 60, "with it gone, freshness reflects the live target"


async def test_the_page_stops_showing_targets_nobody_watches(tmp_path) -> None:
    """The header counted records while the editor counted configured targets,
    so the same page said 6 and 4 at once."""
    sm = await _state_with(tmp_path)
    assert len(sm._state) == 2
    await sm.prune([KEPT])
    assert len(sm._state) == 1, "one configured target, one row"


# =============================================================================
# C. Wired into the run
# =============================================================================


async def test_a_full_run_prunes_what_it_no_longer_probes(tmp_path, monkeypatch) -> None:
    """Pruning belongs at the END of a run: a target dropped mid-run has
    already been probed, and clearing it first would discard work that was
    done. This asserts the wiring, not just the method."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "schema_version": main.STATE_SCHEMA_VERSION,
        "targets": {
            REMOVED: {"status": "DOWN", "last_checked": "2020-01-01T00:00:00Z",
                      "last_changed": "2020-01-01T00:00:00Z", "last_error": "old"},
        },
    }), encoding="utf-8")

    async def _fake_check(url, session, state, **kwargs):
        await state.set_up(url)
        return main.CheckOutcome(url=url, status="UP",
                                 checked_at="2026-01-01T00:00:00Z", phases=None)

    monkeypatch.setattr(main, "check_url", _fake_check)
    await main.run_health_checks(
        targets=[main.Target(url=KEPT)],
        state_path=state_file,
        history_path=tmp_path / "history.db",
    )

    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert KEPT in stored["targets"]
    assert REMOVED not in stored["targets"], "the run must drop what it no longer probes"
