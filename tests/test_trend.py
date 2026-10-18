"""
tests/test_trend.py — "is this getting worse", which no threshold can answer.

The gap this closes
-------------------
Every threshold in this project is absolute: RTT over 400 ms, TTFB over 1500.
Those answer "is this slow", and the per-target overrides already make that
question fair. None of them answers "is this getting worse", and that is the
failure a threshold structurally cannot see: a service drifting from 50 ms to
380 ms over two weeks never crosses 400, so it dies slowly and in silence.

Comparing a target against its own recent past makes the signal relative to the
target. An endpoint that was always 300 ms is not news; one that went from 50
to 300 is.

Two floors keep it quiet, and BOTH are reported rather than swallowed. A +200%
jump from 2 ms to 6 ms is real and irrelevant, but "suppressed because it was
tiny" and "the check never ran" look identical from outside — so the verdict
carries which floor stopped it, and the report prints the number that was too
small. A quiet feature that cannot prove it ran is indistinguishable from a
broken one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import (
    TREND_DEGRADING,
    TREND_IMPROVING,
    TREND_MIN_CHANGE_MS,
    TREND_MIN_CHANGE_PCT,
    TREND_MIN_SAMPLES,
    TREND_MIN_SUCCESS_PCT,
    TREND_MAX_SUCCESS_GAP_PCT,
    TREND_INCOMPARABLE,
    TREND_NO_DATA,
    TREND_OK,
    TREND_TOO_FEW_SAMPLES,
    ProbePhases,
    latency_trend,
)
from history import HistoryRecorder, latency_trend_windows

URL = "https://slow.example"
N   = TREND_MIN_SAMPLES + 10


# =============================================================================
# A. The verdict, as a pure function
# =============================================================================


def test_a_slow_slide_no_threshold_would_catch_is_reported() -> None:
    """The whole point. 50ms to 380ms never crosses a 400ms threshold, so the
    absolute check stays silent while the service dies."""
    t = latency_trend(380.0, 50.0, N, N)
    assert t["verdict"] == TREND_DEGRADING
    assert t["change_ms"] == pytest.approx(330.0)
    assert t["change_pct"] == pytest.approx(660.0)


@pytest.mark.parametrize("steady_ms", [300.0, 1000.0, 3000.0])
def test_a_target_that_was_always_slow_is_not_news(steady_ms) -> None:
    """Relative to the target, not to a global number. 3000ms forever is a
    property of that endpoint, not an incident.

    Parametrised well ABOVE any absolute threshold in this project on purpose:
    a version that secretly compared against a fixed number instead of the
    previous window would call a permanently-slow target "degrading", and only
    a baseline far from that number exposes it.
    """
    t = latency_trend(steady_ms, steady_ms, N, N)
    assert t["verdict"] == TREND_OK
    assert t["change_ms"] == 0.0, "no change means no change, at any speed"


def test_getting_better_is_named_not_ignored() -> None:
    """Recovery is information too -- it tells an operator a fix landed."""
    assert latency_trend(50.0, 300.0, N, N)["verdict"] == TREND_IMPROVING


# =============================================================================
# B. The floors, and the fact that they announce themselves
# =============================================================================


def test_a_huge_percentage_on_tiny_numbers_is_suppressed() -> None:
    """2ms to 6ms is +200% and matters to nobody. Without an absolute floor
    this feature would fire on numbers too small to act on, and a signal that
    fires on nothing gets ignored -- the failure the project is built against."""
    t = latency_trend(6.0, 2.0, N, N)
    assert t["verdict"] == TREND_OK
    assert t["suppressed_by"] == "absolute"


def test_a_big_absolute_change_that_is_proportionally_small_is_suppressed() -> None:
    """5000ms to 5200ms is +200ms -- over the absolute floor -- but only +4%.
    On an endpoint that slow, that is noise."""
    t = latency_trend(5200.0, 5000.0, N, N)
    assert t["verdict"] == TREND_OK
    assert t["suppressed_by"] == "relative"


def test_a_suppressed_change_still_reports_its_numbers() -> None:
    """What the operator asked for: a change too small to act on must still be
    visible, or "no alert" cannot be told from "no check". The numbers travel
    with the verdict."""
    t = latency_trend(6.0, 2.0, N, N)
    assert t["change_pct"] == pytest.approx(200.0)
    assert t["change_ms"] == pytest.approx(4.0)
    assert t["recent_p95_ms"] == 6.0 and t["previous_p95_ms"] == 2.0


def test_both_floors_must_clear_not_either() -> None:
    """AND, not OR. Either floor alone lets through a class of noise the other
    was added to stop."""
    just_over = TREND_MIN_CHANGE_MS + 1
    assert latency_trend(
        just_over + 1000, 1000.0, N, N
    )["suppressed_by"] == "relative", "big in ms, small in %"
    assert latency_trend(
        TREND_MIN_CHANGE_PCT, 1.0, N, N
    )["suppressed_by"] == "absolute", "big in %, small in ms"


# =============================================================================
# C. Refusing to answer, out loud
# =============================================================================


def test_too_few_samples_says_so_rather_than_guessing() -> None:
    """A p95 over three readings is a number, not a measurement. Comparing two
    of them is arithmetic on noise dressed up as a finding."""
    t = latency_trend(380.0, 50.0, 3, 3)
    assert t["verdict"] == TREND_TOO_FEW_SAMPLES
    assert t["change_pct"] is None, "no number is offered for a comparison not made"


def test_one_thin_window_is_enough_to_refuse() -> None:
    """A fat recent window against a thin old one is not a comparison."""
    assert latency_trend(380.0, 50.0, N, 2)["verdict"] == TREND_TOO_FEW_SAMPLES


@pytest.mark.parametrize("recent,previous", [
    (None, 50.0), (380.0, None), (None, None), (380.0, 0.0),
])
def test_a_missing_or_zero_baseline_is_no_data(recent, previous) -> None:
    """A window with no measurable rows -- a target DOWN throughout -- has
    nothing to compare. Zero is excluded too: dividing by it is not a trend."""
    assert latency_trend(recent, previous, N, N)["verdict"] == TREND_NO_DATA


def test_the_verdict_is_never_silent() -> None:
    """Every input produces a verdict. The function has no path that returns
    None, because a caller cannot tell None apart from "nothing was wrong"."""
    for args in [(None, None, 0, 0), (1.0, 1.0, 0, 0), (380.0, 50.0, N, N)]:
        assert latency_trend(*args)["verdict"] is not None


# =============================================================================
# D. Against real stored history
# =============================================================================


def _seed(db: Path, days_ago: float, ttfb: float, count: int) -> None:
    now = datetime.now(timezone.utc)
    outcomes = [
        main.CheckOutcome(
            url=URL, status="UP",
            checked_at=(now - timedelta(days=days_ago, minutes=i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            phases=ProbePhases(url=URL, http_status=200, ttfb_ms=ttfb + (i % 3),
                               transfer_ms=1.0, body_bytes=100),
        )
        for i in range(count)
    ]
    asyncio.run(HistoryRecorder(db).record_run(
        now.strftime("%Y-%m-%dT%H:%M:%SZ"), outcomes))


def test_the_two_windows_do_not_overlap(tmp_path: Path) -> None:
    """The previous window ends exactly where the recent one starts. Overlap
    would mix the readings being compared into both sides of the comparison
    and damp the very change it exists to surface."""
    db = tmp_path / "history.db"
    _seed(db, days_ago=10, ttfb=50.0,  count=N)
    _seed(db, days_ago=2,  ttfb=380.0, count=N)

    recent, previous = latency_trend_windows(db, URL, window_days=7)
    assert recent["p95_ttfb_ms"] > 370, "only the slow rows"
    assert previous["p95_ttfb_ms"] < 60, "only the fast rows"
    assert recent["samples"] == previous["samples"] == N


def test_a_real_degradation_is_found_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    _seed(db, days_ago=10, ttfb=50.0,  count=N)
    _seed(db, days_ago=2,  ttfb=380.0, count=N)

    recent, previous = latency_trend_windows(db, URL, window_days=7)
    verdict = latency_trend(
        recent["p95_ttfb_ms"], previous["p95_ttfb_ms"],
        recent["samples"], previous["samples"],
    )
    assert verdict["verdict"] == TREND_DEGRADING


def test_a_missing_database_refuses_instead_of_crashing(tmp_path: Path) -> None:
    """--report runs against whatever exists. A fresh instance has no history,
    and that must read as "cannot say" rather than take the report down."""
    recent, previous = latency_trend_windows(tmp_path / "nope.db", URL)
    assert recent is None and previous is None


# =============================================================================
# E. Comparability — is this a change at all, before is it a big one
# =============================================================================


def test_the_gap_threshold_is_reachable_given_the_floor() -> None:
    """The two constants are coupled, and this is the test that proves it.

    Both windows must clear TREND_MIN_SUCCESS_PCT before the gap is consulted,
    so they are both inside [floor, 100] and the widest reachable gap is
    100 - floor. A gap threshold above that can never fire.

    The first version of this shipped at 20 points with a floor of 90 — numbers
    taken from httpbin's measured peaks, which was the right measurement for the
    wrong question, because httpbin never clears the floor and so never reaches
    this check. It was dead code that read as a working guard.
    """
    reachable = 100.0 - TREND_MIN_SUCCESS_PCT
    assert 0 < TREND_MAX_SUCCESS_GAP_PCT < reachable, (
        f"a gap of {TREND_MAX_SUCCESS_GAP_PCT} can never fire with a floor of "
        f"{TREND_MIN_SUCCESS_PCT}: both windows clear the floor first, so the "
        f"widest gap possible is {reachable}"
    )


def test_an_unreliable_window_is_not_compared_at_all() -> None:
    """A p95 over a window that lost a quarter of its probes describes the
    survivors. The change here is +660%, and it is still not a verdict."""
    t = latency_trend(380.0, 50.0, N, N, recent_total=40, previous_total=40)
    assert t["verdict"] == TREND_INCOMPARABLE
    assert t["suppressed_by"] == "success_floor"


def test_two_windows_that_answered_differently_are_not_compared() -> None:
    """Both clear the floor, and they still measured different populations.

    100% against 93% is a 7-point gap: the p95 may have moved because a
    different subset of probes came back, not because the backend slowed down.
    """
    t = latency_trend(380.0, 50.0, 30, 28, recent_total=30, previous_total=30)
    assert t["verdict"] == TREND_INCOMPARABLE
    assert t["suppressed_by"] == "success_gap"


def test_comparable_windows_still_produce_a_verdict() -> None:
    """The guard must not swallow the feature it protects."""
    t = latency_trend(380.0, 50.0, N, N, recent_total=N, previous_total=N)
    assert t["verdict"] == TREND_DEGRADING


def test_the_refusal_reports_the_rates_that_caused_it() -> None:
    """"Not comparable" without the numbers reads as a broken check, which is
    the failure this whole feature is written against."""
    t = latency_trend(380.0, 50.0, N, N, recent_total=40, previous_total=40)
    assert t["recent_success_pct"] == pytest.approx(75.0)
    assert t["previous_success_pct"] == pytest.approx(75.0)
    # And the change it refused to judge is still on the record.
    assert t["change_ms"] == pytest.approx(330.0)


def test_without_denominators_the_behaviour_is_unchanged() -> None:
    """The totals are optional so every existing caller keeps working. A caller
    that cannot supply them has nothing to check, not a reason to refuse."""
    assert latency_trend(380.0, 50.0, N, N)["verdict"] == TREND_DEGRADING
    assert latency_trend(50.0, 380.0, N, N)["verdict"] == TREND_IMPROVING


# =============================================================================
# E. The report line
# =============================================================================


def test_every_target_gets_a_line_even_the_boring_ones(tmp_path: Path) -> None:
    """A section that lists only problems cannot be told apart from a section
    that failed to run."""
    db = tmp_path / "history.db"
    _seed(db, days_ago=10, ttfb=50.0, count=N)
    _seed(db, days_ago=2,  ttfb=51.0, count=N)

    report = main._generate_report([main.Target(url=URL)], history_path=db)
    assert "p95 trend" in report
    assert URL in report.split("p95 trend")[1]


def test_the_report_names_the_floor_that_silenced_a_change(tmp_path: Path) -> None:
    """The operator's requirement, asserted: a suppressed change must say WHY,
    so nobody reads it as an oversight."""
    db = tmp_path / "history.db"
    _seed(db, days_ago=10, ttfb=2.0, count=N)
    _seed(db, days_ago=2,  ttfb=6.0, count=N)

    tail = main._generate_report([main.Target(url=URL)], history_path=db)
    tail = tail.split("p95 trend")[1]
    assert "stable" in tail
    assert "minimum" in tail, "the floor must be named"
