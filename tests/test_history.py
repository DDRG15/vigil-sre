"""
tests/test_history.py — Test suite for history.py (an earlier release persistence layer).

Coverage:
  A. Schema / construction        — 3 tests
  B. record_run()                 — 5 tests
  C. prune()                      — 4 tests
  D. _retention_days_from_env()   — 4 tests
  E. Metric contract (caplog)     — 3 tests
  F. uptime_pct() / latency_percentiles() (an earlier release + audit fixes) — 17 tests

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

import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
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
    latency_percentiles,
    uptime_pct,
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
    await rec.record_run("2020-01-01T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2020-01-01T00:00:00Z", phases=_phases())
    ])
    await rec.record_run("2026-07-25T07:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-25T07:00:00Z", phases=_phases())
    ])

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
        CheckOutcome(url=URL, status="DEGRADED", checked_at="2020-01-01T00:00:00Z",
                     phases=_phases(), findings=[finding])
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


async def test_prune_uses_the_findings_index_not_a_full_scan(tmp_path: Path) -> None:
    """Performance regression test for idx_probe_findings_probe_id.

    Without that index, ON DELETE CASCADE full-scans probe_findings once per
    deleted probe_results row -- see the SCHEMA comment in history.py for the
    348x measurement that motivated adding it. A schema edit that silently
    drops the index would pass every correctness test above and only surface
    as a production incident the first time someone lowers
    HISTORY_RETENTION_DAYS against an established database. This seeds a
    database at that scale (5,000 probe_results rows, 3 findings each, 25%
    due for pruning) and asserts the delete finishes fast enough that the
    index must be in use -- calibrated against a real run on this machine:
    ~173ms indexed vs ~2325ms full-scan, a >10x margin either side of the
    threshold below.
    """
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path, retention_days=30)  # creates schema, incl. the index

    now = datetime.now(timezone.utc)
    old_checked_at = "2000-01-01T00:00:00Z"  # older than retention -> pruned
    recent_checked_at = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # kept

    old_rows, recent_rows = 1250, 3750
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executemany(
        "INSERT INTO probe_results (run_started_at, url, checked_at, status) VALUES (?,?,?,'UP')",
        [(ts, URL, ts) for ts in [old_checked_at] * old_rows + [recent_checked_at] * recent_rows],
    )
    # Fresh table, single-threaded insert -> ids are 1..5000 in insertion order.
    con.executemany(
        "INSERT INTO probe_findings (probe_id, code, severity, evidence) VALUES (?,'X','info','')",
        [(probe_id,) for probe_id in range(1, old_rows + recent_rows + 1) for _ in range(3)],
    )
    con.commit()
    con.close()

    start = time.monotonic()
    await rec.prune()
    elapsed = time.monotonic() - start

    con = sqlite3.connect(db_path)
    remaining_results = con.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0]
    remaining_findings = con.execute("SELECT COUNT(*) FROM probe_findings").fetchone()[0]
    con.close()

    assert remaining_results == recent_rows
    assert remaining_findings == recent_rows * 3
    assert elapsed < 1.5, (
        f"prune() took {elapsed:.3f}s on 5,000 rows -- this is the signature of "
        f"a full-scan CASCADE delete. Check idx_probe_findings_probe_id is still "
        f"in SCHEMA (history.py)."
    )


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


# =============================================================================
# E. Metric contract (event_type=metric)
# =============================================================================
# An operator alerting rule scrapes these log lines by their metric= suffix,
# not by the surrounding prose. A refactor that reworded a message but kept
# behaviour correct would pass every test above and still silently zero out
# an alert — the same class of gap the E2 webhook tests (test_main.py) close
# for send_discord_alert. Same standard applies here.


async def test_record_run_logs_rows_written_metric(tmp_path: Path, caplog) -> None:
    rec = HistoryRecorder(tmp_path / "history.db")
    outcomes = [
        CheckOutcome(url=URL, status="UP", phases=_phases()),
        CheckOutcome(url="https://other.example", status="DOWN", error="Timeout (>5s)"),
    ]
    with caplog.at_level("INFO"):
        await rec.record_run("2026-07-25T07:00:00Z", outcomes)
    assert any(
        "event_type=metric metric=history_rows_written_total value=2" in r.message
        for r in caplog.records
    )


async def test_record_run_logs_write_failure_metric(tmp_path: Path, caplog) -> None:
    rec = HistoryRecorder(tmp_path / "history.db")
    with patch.object(rec, "_record_run_sync", side_effect=sqlite3.OperationalError("disk full")):
        with caplog.at_level("ERROR"):
            await rec.record_run("2026-07-25T07:00:00Z", [CheckOutcome(url=URL, status="UP")])
    assert any(
        "event_type=metric metric=history_write_failures_total value=1 reason=write" in r.message
        for r in caplog.records
    )


async def test_prune_logs_rows_pruned_metric(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path, retention_days=30)
    await rec.record_run("2020-01-01T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2020-01-01T00:00:00Z", phases=_phases())
    ])
    with caplog.at_level("INFO"):
        await rec.prune()
    assert any(
        "event_type=metric metric=history_rows_pruned_total value=1" in r.message
        for r in caplog.records
    )


# =============================================================================
# F. uptime_pct() / latency_percentiles() (an earlier release — --report)
#
# The risk this section guards against is arithmetic, not exceptions: a
# wrong uptime number looks exactly like a right one until a human trusts it
# and gets burned. Every assertion below is checked against a value computed
# by hand, not against whatever the implementation happens to produce.
# =============================================================================

OLD_CUTOFF = "2020-01-01T00:00:00Z"


async def test_uptime_pct_hand_calculated(tmp_path: Path) -> None:
    """7 UP + 2 DEGRADED + 1 DOWN out of 10 rows must read exactly 90.0 —
    not approximately, not rounded early."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    statuses = ["UP"] * 7 + ["DEGRADED"] * 2 + ["DOWN"] * 1
    outcomes = [
        CheckOutcome(url=URL, status=s, checked_at="2026-07-30T00:00:00Z",
                     phases=_phases() if s != "DOWN" else None)
        for s in statuses
    ]
    await rec.record_run("2026-07-30T00:00:00Z", outcomes)
    assert uptime_pct(db_path, URL, OLD_CUTOFF) == 90.0


async def test_uptime_pct_degraded_counts_as_up(tmp_path: Path) -> None:
    """Decision this module documents explicitly: DEGRADED is an availability
    success (up and hurting), not a down. All-DEGRADED must read 100%, not 0%."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    outcomes = [
        CheckOutcome(url=URL, status="DEGRADED", checked_at="2026-07-30T00:00:00Z", phases=_phases())
        for _ in range(5)
    ]
    await rec.record_run("2026-07-30T00:00:00Z", outcomes)
    assert uptime_pct(db_path, URL, OLD_CUTOFF) == 100.0


async def test_uptime_pct_no_rows_returns_none_not_zero(tmp_path: Path) -> None:
    """A target with zero rows in the window must read as 'no data' upstream,
    never as 0% -- 0% reads as 'down the entire window', which is a stronger
    and false claim about a target nobody has probed yet."""
    db_path = tmp_path / "history.db"
    HistoryRecorder(db_path)  # only initialises the schema, writes nothing
    assert uptime_pct(db_path, URL, OLD_CUTOFF) is None


async def test_uptime_pct_no_schema_yet_returns_none_not_a_crash(tmp_path: Path) -> None:
    """Reproduced live: running --report before HistoryRecorder has ever been
    constructed (a freshly deployed instance, or a check before the first
    probe cycle) points at a history.db with no schema at all -- 'no such
    table', not just 'no rows'. That must read as no data too, not a
    traceback surfaced straight to the operator's terminal."""
    db_path = tmp_path / "history.db"  # never touched -- no schema, maybe no file
    assert uptime_pct(db_path, URL, OLD_CUTOFF) is None


async def test_latency_percentiles_no_schema_yet_returns_none_not_a_crash(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    assert latency_percentiles(db_path, URL, OLD_CUTOFF) is None


async def test_uptime_pct_raises_on_a_locked_database_instead_of_saying_no_data(
    tmp_path: Path,
) -> None:
    """'Nothing recorded yet' and 'I could not read it' are different claims.
    Reproduced during the an earlier release audit: running --report while the probe
    loop held a write lock reported 'no data' for a target with 100% uptime
    and real rows, sending the operator hunting a persistence bug in a
    healthy system. Only 'no such table' may be swallowed."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    await rec.record_run("2026-07-30T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z", phases=_phases())
    ])
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            uptime_pct(db_path, URL, OLD_CUTOFF)
    finally:
        blocker.close()


async def test_latency_percentiles_raises_on_a_locked_database(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    await rec.record_run("2026-07-30T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z", phases=_phases())
    ])
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            latency_percentiles(db_path, URL, OLD_CUTOFF)
    finally:
        blocker.close()


@pytest.mark.parametrize("n", [2, 3, 5, 10, 50])
async def test_percentiles_include_the_maximum_at_every_sample_size(
    tmp_path: Path, n: int,
) -> None:
    """PERCENT_RANK is (rank-1)/(n-1), so the top row always evaluates to
    exactly 1.0 and `pr <= 0.95` structurally excludes the maximum -- biasing
    p95 downward, always in the flattering direction. Measured during the
    audit: 10 samples of 1..10ms reported p95 = 9, and 2 samples reported the
    MINIMUM. Checked here against the textbook nearest-rank value, ceil(p*n),
    at the sample sizes where the distortion is worst."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    await rec.record_run("2026-07-30T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z",
                     phases=_phases(ttfb_ms=float(v)))
        for v in range(1, n + 1)
    ])
    result = latency_percentiles(db_path, URL, OLD_CUTOFF)
    assert result == {
        "p50_ttfb_ms": float(math.ceil(0.50 * n)),
        "p95_ttfb_ms": float(math.ceil(0.95 * n)),
    }


async def test_uptime_pct_excludes_rows_before_since(tmp_path: Path) -> None:
    """A row outside the requested window must not count toward the
    percentage at all -- not as up, not as down."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    await rec.record_run("2020-01-01T00:00:00Z", [
        CheckOutcome(url=URL, status="DOWN", checked_at="2020-01-01T00:00:00Z")
    ])
    await rec.record_run("2026-07-30T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z", phases=_phases())
    ])
    # Window starts after the 2020 DOWN row -- only the 2026 UP row counts.
    assert uptime_pct(db_path, URL, "2026-01-01T00:00:00Z") == 100.0


async def test_latency_percentiles_hand_calculated(tmp_path: Path) -> None:
    """20 rows with ttfb_ms = 1..20ms. PERCENT_RANK over n=20 puts p50 (rank
    <=0.50) at the 10th-ranked value and p95 (rank <=0.95) at the 19th --
    computed by hand, not by trusting the query to be self-consistent."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    outcomes = [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z",
                     phases=_phases(ttfb_ms=float(ms)))
        for ms in range(1, 21)
    ]
    await rec.record_run("2026-07-30T00:00:00Z", outcomes)
    result = latency_percentiles(db_path, URL, OLD_CUTOFF)
    assert result == {"p50_ttfb_ms": 10.0, "p95_ttfb_ms": 19.0}


async def test_latency_percentiles_no_rows_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    HistoryRecorder(db_path)
    assert latency_percentiles(db_path, URL, OLD_CUTOFF) is None


async def test_latency_percentiles_ignores_down_rows_with_null_ttfb(tmp_path: Path) -> None:
    """A DOWN row has phases=None -> ttfb_ms NULL in the schema. It must not
    be treated as 0ms (impossibly fast) or crash the window function -- it
    is simply absent from the sample."""
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    outcomes = [CheckOutcome(url=URL, status="DOWN", checked_at="2026-07-30T00:00:00Z")]
    outcomes += [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z",
                     phases=_phases(ttfb_ms=50.0))
        for _ in range(3)
    ]
    await rec.record_run("2026-07-30T00:00:00Z", outcomes)
    result = latency_percentiles(db_path, URL, OLD_CUTOFF)
    assert result == {"p50_ttfb_ms": 50.0, "p95_ttfb_ms": 50.0}


async def test_latency_percentiles_excludes_rows_before_since(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    rec = HistoryRecorder(db_path)
    await rec.record_run("2020-01-01T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2020-01-01T00:00:00Z",
                     phases=_phases(ttfb_ms=9999.0))
    ])
    await rec.record_run("2026-07-30T00:00:00Z", [
        CheckOutcome(url=URL, status="UP", checked_at="2026-07-30T00:00:00Z",
                     phases=_phases(ttfb_ms=50.0))
    ])
    result = latency_percentiles(db_path, URL, "2026-01-01T00:00:00Z")
    assert result == {"p50_ttfb_ms": 50.0, "p95_ttfb_ms": 50.0}
