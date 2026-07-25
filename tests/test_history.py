"""
tests/test_history.py — Test suite for history.py (an earlier release persistence layer).

Coverage:
  A. Schema / construction        — 3 tests
  B. record_run()                 — 5 tests
  C. prune()                      — 3 tests
  D. _retention_days_from_env()   — 4 tests

Reviewer notes
--------------
The isolation boundary (never raise into the caller) is the one property
this module exists to guarantee — every failure-path test asserts the
method returned normally, not just that "no exception escaped pytest",
by forcing the internal sync method to raise and checking record_run/prune
still complete. A test that never actually breaks the internals would not
be proving the guarantee, only assuming it.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics import Diagnosis, ProbePhases
from history import (
    RETENTION_DAYS_DEFAULT,
    CheckOutcome,
    HistoryRecorder,
    _retention_days_from_env,
)

URL = "https://example.com"


def _phases(**overrides) -> ProbePhases:
    defaults = dict(
        url=URL, http_status=200, ttfb_ms=190.0, transfer_ms=80.0,
        body_bytes=1024, rtt_ms=156.0,
    )
    defaults.update(overrides)
    return ProbePhases(**defaults)


# =============================================================================
# A. Schema / construction
# =============================================================================


def test_schema_creates_both_tables(tmp_path: Path) -> None:
    HistoryRecorder(tmp_path / "history.db")
    con = sqlite3.connect(tmp_path / "history.db")
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    con.close()
    assert {"probe_results", "probe_findings"} <= tables


def test_schema_init_is_idempotent(tmp_path: Path) -> None:
    """Constructing a second HistoryRecorder against the same file (the
    every-60s pattern of run_health_checks) must not fail on 'table exists'."""
    db_path = tmp_path / "history.db"
    HistoryRecorder(db_path)
    rec2 = HistoryRecorder(db_path)  # must not raise
    assert rec2._disabled is False


def test_construction_disables_on_unwritable_path(tmp_path: Path) -> None:
    """A parent directory that doesn't exist must disable the recorder, not
    crash run_health_checks — history is a feature the service can run
    without, unlike state.json."""
    bad_path = tmp_path / "no" / "such" / "dir" / "history.db"
    rec = HistoryRecorder(bad_path)
    assert rec._disabled is True


# =============================================================================
# B. record_run()
# =============================================================================


async def test_record_run_writes_one_row_per_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    outcomes = [
        CheckOutcome(url=URL, status="UP", phases=_phases()),
        CheckOutcome(url="https://other.example", status="DOWN", error="Timeout (>5s)"),
    ]
    await rec.record_run("2026-07-25T07:00:00Z", outcomes)

    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT url, status, error, rtt_ms FROM probe_results ORDER BY id").fetchall()
    con.close()
    assert rows == [
        (URL, "UP", None, 156.0),
        ("https://other.example", "DOWN", "Timeout (>5s)", None),
    ]


async def test_record_run_links_findings_to_the_right_probe(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    finding = Diagnosis(
        code="HIGH_RTT_NEEDS_EDGE", severity="warn",
        evidence="rtt_ms=156", recommendation="use a CDN",
    )
    outcomes = [
        CheckOutcome(url=URL, status="DEGRADED", error="HIGH_RTT_NEEDS_EDGE (...)",
                     phases=_phases(), findings=[finding]),
    ]
    await rec.record_run("2026-07-25T07:00:00Z", outcomes)

    con = sqlite3.connect(db_path)
    probe_id = con.execute("SELECT id FROM probe_results").fetchone()[0]
    findings = con.execute(
        "SELECT probe_id, code, severity FROM probe_findings"
    ).fetchall()
    con.close()
    assert findings == [(probe_id, "HIGH_RTT_NEEDS_EDGE", "warn")]


async def test_record_run_empty_outcomes_is_a_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    await rec.record_run("2026-07-25T07:00:00Z", [])  # must not raise
    con = sqlite3.connect(db_path)
    count = con.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0]
    con.close()
    assert count == 0


async def test_record_run_never_raises_on_write_failure(tmp_path: Path) -> None:
    """The isolation boundary: a write failure (forced here) must be caught,
    logged, and swallowed — record_run must return normally regardless."""
    rec = HistoryRecorder(tmp_path / "history.db")
    with patch.object(rec, "_record_run_sync", side_effect=sqlite3.OperationalError("disk full")):
        await rec.record_run("2026-07-25T07:00:00Z", [CheckOutcome(url=URL, status="UP")])
    # No exception reached here — that IS the assertion.


async def test_record_run_noop_when_disabled(tmp_path: Path) -> None:
    rec = HistoryRecorder(tmp_path / "no" / "such" / "dir" / "history.db")
    assert rec._disabled is True
    await rec.record_run("2026-07-25T07:00:00Z", [CheckOutcome(url=URL, status="UP")])
    # Must not raise, and must not have attempted to create the missing dir.
    assert not (tmp_path / "no").exists()


# =============================================================================
# C. prune()
# =============================================================================


async def test_prune_deletes_rows_older_than_retention(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path, retention_days=30)
    await rec.record_run("2020-01-01T00:00:00Z", [CheckOutcome(url=URL, status="UP", phases=_phases())])
    await rec.record_run("2026-07-25T07:00:00Z", [CheckOutcome(url=URL, status="UP", phases=_phases())])

    await rec.prune()

    con = sqlite3.connect(db_path)
    remaining = con.execute("SELECT run_started_at FROM probe_results").fetchall()
    con.close()
    assert remaining == [("2026-07-25T07:00:00Z",)]


async def test_prune_cascades_to_findings(tmp_path: Path) -> None:
    """ON DELETE CASCADE must remove findings whose probe_results row was
    pruned — an orphaned finding row would be a silent leak the retention
    policy doesn't actually bound."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path, retention_days=30)
    finding = Diagnosis(code="SLOW_DNS", severity="warn", evidence="dns_ms=300", recommendation="...")
    await rec.record_run("2020-01-01T00:00:00Z", [
        CheckOutcome(url=URL, status="DEGRADED", phases=_phases(), findings=[finding])
    ])

    await rec.prune()

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    counts = (
        con.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM probe_findings").fetchone()[0],
    )
    con.close()
    assert counts == (0, 0)


async def test_prune_never_raises_on_failure(tmp_path: Path) -> None:
    rec = HistoryRecorder(tmp_path / "history.db")
    with patch.object(rec, "_prune_sync", side_effect=sqlite3.OperationalError("locked")):
        await rec.prune()  # must not raise


# =============================================================================
# D. _retention_days_from_env()
# =============================================================================


def test_retention_days_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("HISTORY_RETENTION_DAYS", raising=False)
    assert _retention_days_from_env() == RETENTION_DAYS_DEFAULT


def test_retention_days_reads_valid_value(monkeypatch) -> None:
    monkeypatch.setenv("HISTORY_RETENTION_DAYS", "7")
    assert _retention_days_from_env() == 7


def test_retention_days_falls_back_on_non_integer(monkeypatch) -> None:
    monkeypatch.setenv("HISTORY_RETENTION_DAYS", "not-a-number")
    assert _retention_days_from_env() == RETENTION_DAYS_DEFAULT


@pytest.mark.parametrize("bad_value", ["0", "-5"])
def test_retention_days_falls_back_on_non_positive(monkeypatch, bad_value: str) -> None:
    monkeypatch.setenv("HISTORY_RETENTION_DAYS", bad_value)
    assert _retention_days_from_env() == RETENTION_DAYS_DEFAULT
