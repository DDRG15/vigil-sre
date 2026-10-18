"""
tests/test_strip_hysteresis.py -- the strip must not paint a failed probe green.

Inside the DOWN-confirmation window a failed probe keeps the previous healthy
status in its row on purpose (status UP or DEGRADED), with ttfb_ms NULL because
the failure path returns no phases. uptime_pct already reads ttfb_ms IS NULL as
"did not answer"; the strip mapped on ``status`` alone, so one row was 0.0 in
the percentage and green on the strip. These tests hold the two readings of
the same row together.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics import ProbePhases
from history import HistoryRecorder, status_strip, uptime_pct
from main import CheckOutcome

URL = "https://example.com"


def _stamp(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _since(days: int = 7) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _phases() -> ProbePhases:
    return ProbePhases(
        url=URL, http_status=200, ttfb_ms=10.0, transfer_ms=1.0,
        body_bytes=100, rtt_ms=5.0,
    )


def _seed(db: Path, rows: list[tuple[str, bool, float]]) -> None:
    """rows = [(status, answered, minutes_ago)]. answered=False writes the
    hysteresis shape: the status column keeps its healthy value, no phases."""
    outcomes = [
        CheckOutcome(
            url=URL, status=status, checked_at=_stamp(ago),
            phases=_phases() if answered else None,
            error=None if answered else "connection refused",
        )
        for status, answered, ago in rows
    ]
    asyncio.run(HistoryRecorder(db).record_run(_stamp(0), outcomes))


def test_a_failed_probe_with_a_kept_up_status_paints_down(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, [("UP", False, 1)])
    con = sqlite3.connect(db)
    try:
        status, ttfb = con.execute("SELECT status, ttfb_ms FROM probe_results").fetchone()
    finally:
        con.close()
    assert (status, ttfb) == ("UP", None), "precondition: the hysteresis row shape"
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["DOWN"]


def test_strip_and_uptime_agree_about_the_same_row(tmp_path: Path) -> None:
    """The defect was a disagreement, so assert the agreement."""
    db = tmp_path / "history.db"
    _seed(db, [("UP", False, 1)])
    assert uptime_pct(db, URL, _since()) == 0.0
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["DOWN"]


def test_a_failed_probe_with_a_kept_degraded_status_paints_down(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, [("DEGRADED", False, 1)])
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["DOWN"]


def test_a_real_degraded_row_still_paints_degraded(tmp_path: Path) -> None:
    """DEGRADED carries a TTFB. It is slow, not unavailable, and must not be
    swept into DOWN by the NULL rule."""
    db = tmp_path / "history.db"
    _seed(db, [("DEGRADED", True, 1)])
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["DEGRADED"]


def test_a_real_up_row_still_paints_up(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, [("UP", True, 1)])
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["UP"]


def test_one_failed_probe_among_answered_ones_makes_the_bucket_down(tmp_path: Path) -> None:
    """Worst wins: a hysteresis-window failure inside a bucket of healthy
    probes is the short outage the strip exists to show."""
    db = tmp_path / "history.db"
    _seed(db, [("UP", True, 1), ("UP", False, 2), ("UP", True, 3)])
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["DOWN"]


def test_an_empty_bucket_stays_none_not_down(tmp_path: Path) -> None:
    """No data is its own muted state. The NULL rule applies to rows, never to
    the absence of rows."""
    db = tmp_path / "history.db"
    _seed(db, [("UP", False, 1)])
    strip = status_strip(db, [URL], _since(), buckets=10)[URL]
    assert strip[-1] == "DOWN"
    assert all(slot is None for slot in strip[:-1])
