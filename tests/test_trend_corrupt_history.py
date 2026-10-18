"""
tests/test_trend_corrupt_history.py -- a history.db that is not a database must
cost the round its trend check and nothing else.

_maybe_alert_trend caught sqlite3.OperationalError only. A file that is not a
SQLite database (a truncated copy, a partial restore, a text file dropped on the
volume) raises sqlite3.DatabaseError -- a sibling, not a subclass -- and that
escaped the awaited call in run_health_checks. By then every status alert had
been sent, so the round looked fine from outside, but it died before
state.prune() and before the heartbeat: the watchdog went red for a corrupt
advisory file, and the same corrupt file made the next round do the same.

The trend is advisory. The probing it advises on is not.

The marker is the other half. record_trend_alert(None) means "the drift is over,
arm the next episode"; it may only be written on the strength of a verdict. A
read that failed has no verdict, so the marker must be exactly as it was.

Coverage:
  A. _maybe_alert_trend: corrupt file, no raise, marker untouched, logged once
  B. The whole round still prunes and sends its heartbeat
  C. --report's trend section degrades instead of crashing
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import ProbePhases
from main import Target

URL   = "https://drifting.example"
OTHER = "https://other.example"


def _corrupt_db(tmp_path: Path) -> Path:
    db = tmp_path / "history.db"
    db.write_bytes(b"this is definitely not a sqlite database\n" * 200)
    return db


class _Spy:
    """Stands in for _alert_unless_muted and remembers what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, session, url, detail, kind, maintenance=None, state=None):
        self.sent.append(url)
        return main.AlertOutcome.DELIVERED


async def _state_with_marker(tmp_path: Path, *urls: str) -> main.StateManager:
    state = main.StateManager(tmp_path / "state.json")
    for url in urls:
        await state.set_up(url)
        await state.record_trend_alert(url, 412.0)
    return state


# =============================================================================
# A. _maybe_alert_trend
# =============================================================================


async def test_a_corrupt_history_file_is_the_exact_error_that_escaped(tmp_path) -> None:
    """Pins the premise: DatabaseError is not an OperationalError."""
    with pytest.raises(sqlite3.DatabaseError) as raised:
        main.latency_trend_windows(_corrupt_db(tmp_path), URL, 7, None)
    assert not isinstance(raised.value, sqlite3.OperationalError)


async def test_a_corrupt_history_file_does_not_raise_out_of_the_trend_check(
        tmp_path, monkeypatch) -> None:
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state = await _state_with_marker(tmp_path, URL)

    delivered = await main._maybe_alert_trend(
        [Target(url=URL)], state, _corrupt_db(tmp_path))

    assert delivered == 0
    assert spy.sent == []


async def test_a_failed_read_leaves_the_trend_marker_exactly_as_it_was(
        tmp_path, monkeypatch) -> None:
    """No verdict, no change. Clearing the marker here would re-arm the episode
    and send the same trend alert again the moment the file is readable."""
    monkeypatch.setattr(main, "_alert_unless_muted", _Spy())
    state = await _state_with_marker(tmp_path, URL, OTHER)

    await main._maybe_alert_trend(
        [Target(url=URL), Target(url=OTHER)], state, _corrupt_db(tmp_path))

    assert state.trend_alerted_p95(URL) == 412.0
    assert state.trend_alerted_p95(OTHER) == 412.0


async def test_a_corrupt_file_is_logged_once_per_round_not_once_per_target(
        tmp_path, monkeypatch, caplog) -> None:
    """Every target reads the same file and fails the same way; N identical
    lines at 03:00 are one line and a scroll."""
    monkeypatch.setattr(main, "_alert_unless_muted", _Spy())
    state = await _state_with_marker(tmp_path, URL, OTHER)

    with caplog.at_level("ERROR", logger="sre.health_checker"):
        await main._maybe_alert_trend(
            [Target(url=URL), Target(url=OTHER)], state, _corrupt_db(tmp_path))

    lines = [r.getMessage() for r in caplog.records
             if "trend" in r.getMessage().lower()]
    assert len(lines) == 1, lines
    assert "file is not a database" in lines[0]
    assert "event_type=metric metric=trend_checks_skipped_total value=1" in lines[0]


async def test_a_locked_database_is_still_skipped(tmp_path, monkeypatch) -> None:
    """The OperationalError path the earlier guard covered keeps working."""
    def locked(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main, "latency_trend_windows", locked)
    monkeypatch.setattr(main, "_alert_unless_muted", _Spy())
    state = await _state_with_marker(tmp_path, URL)

    assert await main._maybe_alert_trend(
        [Target(url=URL)], state, tmp_path / "h.db") == 0
    assert state.trend_alerted_p95(URL) == 412.0


async def test_only_sqlite_errors_are_swallowed(tmp_path, monkeypatch) -> None:
    """The guard is sqlite3.Error, not Exception: a bug in the trend code must
    still be loud."""
    def bug(*_a, **_kw):
        raise KeyError("a real bug")

    monkeypatch.setattr(main, "latency_trend_windows", bug)
    state = await _state_with_marker(tmp_path, URL)

    with pytest.raises(KeyError):
        await main._maybe_alert_trend([Target(url=URL)], state, tmp_path / "h.db")


# =============================================================================
# B. The round around it
# =============================================================================


async def test_a_round_with_a_corrupt_history_still_prunes_and_sends_the_heartbeat(
        tmp_path, monkeypatch) -> None:
    """The consequence the on-call saw: no prune, no heartbeat, exit non-zero
    for a file that only feeds an advisory alert."""
    async def _probe(*a, **k):
        return ProbePhases(url=URL, http_status=200, ttfb_ms=50.0,
                           transfer_ms=1.0, body_bytes=10)

    async def _no_sample(*a, **k):
        return None

    heartbeat = AsyncMock()
    monkeypatch.setattr(main, "_probe_with_backoff", _probe)
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    monkeypatch.setattr(main, "dispatch_alert", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_send_heartbeat", heartbeat)

    state_path = tmp_path / "state.json"
    seed = main.StateManager(state_path)
    await seed.set_up(URL)
    await seed.set_up("https://removed-from-config.example")

    report = main.RunReport()
    down = await main.run_health_checks(
        targets=[Target(url=URL)], state_path=state_path,
        history_path=_corrupt_db(tmp_path), report=report)

    assert down == 0
    assert "https://removed-from-config.example" not in main.StateManager(state_path)._state, (
        "the round died before state.prune()")
    assert heartbeat.await_count == 1, "the round died before the heartbeat"
    assert main._finish_run(False, down, state_path, report) == 0


# =============================================================================
# C. --report
# =============================================================================


def test_the_report_trend_section_says_so_when_the_history_is_unreadable(
        tmp_path, caplog) -> None:
    """A section that vanishes reads as a section that did not run, and this
    project treats silence that looks like health as the thing to design
    against. It says what happened instead."""
    with caplog.at_level("ERROR", logger="sre.health_checker"):
        lines = main._trend_lines([Target(url=URL)], _corrupt_db(tmp_path))

    text = "\n".join(lines)
    assert "p95 trend" in text
    assert "unreadable" in text and "file is not a database" in text
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1


def test_the_report_trend_section_still_renders_on_a_healthy_file(tmp_path) -> None:
    from history import HistoryRecorder
    db = tmp_path / "history.db"
    asyncio.run(HistoryRecorder(db).record_run("2026-01-01T00:00:00Z", []))

    lines = main._trend_lines([Target(url=URL)], db)

    assert any("not enough data" in line for line in lines)
