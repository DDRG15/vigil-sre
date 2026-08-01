"""
tests/test_status_strip.py — the history strip, from SQL to SVG.

Why this feature exists
-----------------------
The project stored 30 days of probe results and showed three numbers from
them: uptime, p50, p95. "99.2% uptime" and "99.2% uptime, all of it lost in one
three-hour outage the day before yesterday" are different incidents and read
identically as a percentage. The strip answers *when*, which no aggregate can.

The two properties that carry the design, and that these tests exist to hold:

  - **The worst status in a bucket wins.** A bucket spans hours. Averaging it,
    or taking the last reading, would round a short outage away to green --
    which is precisely the event the strip is for.
  - **An empty bucket is not UP.** No data means the monitor was not watching.
    Rendering that as healthy would let a dead monitor's silence read as a
    clean bill of health.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard
from dashboard import COLOURS, STRIP_EMPTY, render_page, render_rows, render_strip
from diagnostics import ProbePhases
from history import HistoryRecorder, status_strip
from main import CheckOutcome

URL   = "https://example.com"
OTHER = "https://other.example"


def _phases(url: str = URL) -> ProbePhases:
    return ProbePhases(
        url=url, http_status=200, ttfb_ms=10.0, transfer_ms=1.0,
        body_bytes=100, rtt_ms=5.0,
    )


def _stamp(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(db: Path, rows: list[tuple[str, str, float]]) -> None:
    """rows = [(url, status, minutes_ago)]"""
    rec = HistoryRecorder(db)
    outcomes = [
        CheckOutcome(
            url=url, status=status, checked_at=_stamp(ago),
            phases=_phases(url) if status != "DOWN" else None,
        )
        for url, status, ago in rows
    ]
    asyncio.run(rec.record_run(_stamp(0), outcomes))


def _since(days: int = 7) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# =============================================================================
# A. Bucketing
# =============================================================================


def test_the_worst_status_in_a_bucket_wins(tmp_path: Path) -> None:
    """One failure among many successes in the same bucket must show as the
    failure. Averaging is what makes a short outage invisible, and a short
    outage is the whole reason to look at a strip instead of a percentage."""
    db = tmp_path / "history.db"
    _seed(db, [(URL, "UP", 1), (URL, "UP", 2), (URL, "DOWN", 3), (URL, "UP", 4)])
    strip = status_strip(db, [URL], _since(), buckets=1)[URL]
    assert strip == ["DOWN"]


def test_degraded_loses_to_down_but_beats_up(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, [(URL, "UP", 1), (URL, "DEGRADED", 2)])
    assert status_strip(db, [URL], _since(), buckets=1)[URL] == ["DEGRADED"]

    db2 = tmp_path / "h2.db"
    _seed(db2, [(URL, "DEGRADED", 1), (URL, "DOWN", 2)])
    assert status_strip(db2, [URL], _since(), buckets=1)[URL] == ["DOWN"]


def test_a_bucket_with_no_data_is_none_not_up(tmp_path: Path) -> None:
    """The design decision of this feature. Time the monitor was not watching
    must not render as time it watched and found nothing wrong."""
    db = tmp_path / "history.db"
    _seed(db, [(URL, "UP", 1)])
    strip = status_strip(db, [URL], _since(), buckets=10)
    assert strip[URL][-1] == "UP", "the recent reading lands in the last bucket"
    assert all(slot is None for slot in strip[URL][:-1]), "the rest is unknown"
    assert "UP" not in strip[URL][:-1]


def test_every_strip_has_exactly_the_requested_length(tmp_path: Path) -> None:
    """A strip whose width changed with how much history exists would make two
    rows incomparable at a glance, which is what a strip is for."""
    db = tmp_path / "history.db"
    _seed(db, [(URL, "UP", 1), (OTHER, "DOWN", 1)])
    strips = status_strip(db, [URL, OTHER], _since(), buckets=37)
    assert len(strips) == 2
    assert all(len(s) == 37 for s in strips.values())


def test_targets_do_not_bleed_into_each_other(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, [(URL, "UP", 1), (OTHER, "DOWN", 1)])
    strips = status_strip(db, [URL, OTHER], _since(), buckets=1)
    assert strips[URL] == ["UP"]
    assert strips[OTHER] == ["DOWN"]


def test_a_target_with_no_rows_gets_a_blank_strip(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, [(URL, "UP", 1)])
    strips = status_strip(db, [URL, OTHER], _since(), buckets=5)
    assert strips[OTHER] == [None] * 5


def test_a_missing_database_is_not_an_error(tmp_path: Path) -> None:
    """A freshly deployed instance has no history.db. mode=ro raises "unable to
    open database file" for a missing path -- a different error from "no such
    table" -- and letting that reach do_GET would 500 the whole dashboard."""
    strips = status_strip(tmp_path / "nope.db", [URL], _since(), buckets=4)
    assert strips == {URL: [None] * 4}


def test_no_targets_is_not_a_query(tmp_path: Path) -> None:
    assert status_strip(tmp_path / "history.db", [], _since()) == {}


# =============================================================================
# B. Rendering
# =============================================================================


def test_one_bar_per_bucket() -> None:
    assert render_strip(["UP", None, "DOWN"]).count("<rect") == 3


def test_an_empty_bucket_is_visually_distinct_from_a_real_status() -> None:
    """Not COLOURS["UNKNOWN"]: an unknown STATUS is something the monitor
    observed and could not classify; an empty bucket is time it was not
    watching. The two must not look alike."""
    assert STRIP_EMPTY != COLOURS["UNKNOWN"]
    markup = render_strip([None])
    assert STRIP_EMPTY in markup
    for colour in COLOURS.values():
        assert f'fill="{colour}"' not in markup


def test_each_bar_carries_its_status_as_text() -> None:
    """Colour is the only channel a 4px bar can carry, so the information has
    to be reachable another way -- by hover and by screen reader."""
    markup = render_strip(["DOWN"])
    assert "<title>DOWN</title>" in markup
    assert 'role="img"' in markup
    assert "aria-label=" in markup


def test_no_strip_renders_nothing_rather_than_an_empty_box() -> None:
    assert render_strip(None) == ""
    assert render_strip([]) == ""


def test_the_page_survives_strips_being_absent() -> None:
    """Every caller before this feature passed three arguments. The renderer
    must not require a fourth to keep working."""
    state = {"status": "UP", "last_checked": "now", "last_error": ""}
    assert "<svg" not in render_page({URL: state}, {}, 10)


def test_a_malformed_strip_does_not_break_the_row() -> None:
    """Same rule the rest of the renderer follows: one bad row must not cost
    the other five."""
    state = {"status": "UP", "last_checked": "now", "last_error": ""}
    markup = render_rows({URL: state}, {}, 10, {URL: "not a list"})
    assert "<details" in markup
    assert "<svg" not in markup
