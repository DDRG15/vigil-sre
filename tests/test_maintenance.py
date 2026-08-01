"""
tests/test_maintenance.py — planned work, without the 3am page.

Why this exists
---------------
Being woken by a monitor for an outage you scheduled yourself is the single
most reliable way to make someone stop trusting it. Every serious monitoring
tool has maintenance windows for that reason.

Two properties carry the whole feature, and both are load-bearing:

  - **The window silences the ALERT, never the PROBE.** During one the target
    is still measured and still recorded, so uptime stays honest and the
    history has no hole. A window that stopped probing would leave a gap on the
    dashboard indistinguishable from "the monitor died" — trading a known noise
    for an unknown silence, which is the worse of the two every time.
  - **A muted alert does not arm the retry.** Fase 23's `alert_pending` re-sends
    anything that reached nobody; if a deliberate silence counted as a failed
    delivery, every suppressed alert would queue and fire the moment the window
    closed. Planned quiet would become an unplanned storm.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard
import targetstore
from diagnostics import in_maintenance
from targetstore import ValidationError, validate_entry

URL   = "https://example.com"
NIGHT = {"start": "02:00", "end": "04:00"}


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """August 2026: the 3rd is a Monday, so weekday() lines up with day-3."""
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


# =============================================================================
# A. When a window is in effect
# =============================================================================


@pytest.mark.parametrize("hour,minute,inside", [
    (1, 59, False),   # one minute before: the alert still fires
    (2, 0,  True),    # start is inclusive
    (3, 30, True),
    (4, 0,  False),   # end is exclusive -- otherwise two adjacent windows overlap
    (5, 0,  False),
])
def test_the_boundaries_are_tested_from_both_sides(hour, minute, inside) -> None:
    """A window that silences is half a test; one that stops silencing on time
    is the other half. An end that never arrives is a target muted forever."""
    assert bool(in_maintenance([NIGHT], _at(3, hour, minute))) is inside


def test_a_window_can_cross_midnight(tmp_path=None) -> None:
    """Maintenance happens when nobody is awake, which is precisely the span
    that crosses midnight. Treating 22:00-02:00 as invalid would refuse the
    most common window there is."""
    window = [{"days": [0], "start": "22:00", "end": "02:00"}]   # Monday night
    assert in_maintenance(window, _at(3, 23))    # Monday 23:00  -> inside
    assert in_maintenance(window, _at(4, 1))     # Tuesday 01:00 -> still inside
    assert not in_maintenance(window, _at(4, 3))  # Tuesday 03:00 -> over
    assert not in_maintenance(window, _at(5, 1))  # Wednesday     -> wrong day


def test_the_tail_after_midnight_belongs_to_the_previous_day() -> None:
    """"Monday 22:00 to 02:00" is how a human says it, and they mean the small
    hours of Tuesday. Matching the config to the sentence avoids an operator
    scheduling Monday and being paged on Tuesday."""
    window = [{"days": [0], "start": "22:00", "end": "02:00"}]
    assert in_maintenance(window, _at(4, 1)), "Tuesday 01:00 is Monday's window"
    assert not in_maintenance(window, _at(3, 1)), "Monday 01:00 is Sunday's, not set"


def test_days_are_honoured() -> None:
    weekend = [{"days": [5, 6], "start": "02:00", "end": "04:00"}]
    assert in_maintenance(weekend, _at(8, 3))      # Saturday
    assert not in_maintenance(weekend, _at(5, 3))  # Wednesday


def test_no_days_means_every_day() -> None:
    assert in_maintenance([NIGHT], _at(3, 3))
    assert in_maintenance([NIGHT], _at(8, 3))


def test_no_windows_never_mutes() -> None:
    """The default has to be "alert me". A target that silently muted itself
    would be the failure this whole project is built against."""
    for empty in (None, []):
        assert in_maintenance(empty, _at(3, 3)) is None


def test_the_window_in_effect_is_returned_not_just_a_boolean() -> None:
    """The dashboard names the window on the row, so the caller needs to know
    WHICH one applies -- "muted" without "until when" leaves the reader with
    the next question unanswered."""
    result = in_maintenance([NIGHT], _at(3, 3))
    assert result["start"] == "02:00" and result["end"] == "04:00"


def test_a_malformed_window_is_skipped_not_fatal() -> None:
    """Validated on the way in, but a hand-edited store or an older version can
    still produce one. Skipping it alerts; crashing takes the probe down."""
    broken = [{"start": "nope", "end": "04:00"}, NIGHT]
    assert in_maintenance(broken, _at(3, 3)) == NIGHT


# =============================================================================
# B. What the store refuses
# =============================================================================


@pytest.mark.parametrize("bad,why", [
    ([{"start": "25:00", "end": "04:00"}],          "hour out of range"),
    ([{"start": "02:60", "end": "04:00"}],          "minute out of range"),
    ([{"start": "2:00",  "end": "04:00"}],          "not zero-padded"),
    ([{"start": "02:00"}],                          "no end"),
    ([{"end": "04:00"}],                            "no start"),
    ([{"start": "02:00", "end": "02:00"}],          "zero-length or 24h, ambiguous"),
    ([{"days": [7], "start": "02:00", "end": "04:00"}], "day out of range"),
    ([{"days": [], "start": "02:00", "end": "04:00"}],  "empty day list"),
    ([{"days": "mon", "start": "02:00", "end": "04:00"}], "days not a list"),
    ([{"start": "02:00", "end": "04:00", "extra": 1}],   "unknown field"),
    ("not a list",                                   "not a list"),
])
def test_an_unusable_window_is_refused(bad, why) -> None:
    """Rejecting matters more here than elsewhere: a window that silently
    failed to parse leaves the operator believing a target is silenced while it
    pages them at 3am -- the promise broken in the direction that costs sleep."""
    with pytest.raises(ValidationError):
        validate_entry({"url": URL, "maintenance": bad})


def test_a_valid_window_round_trips() -> None:
    entry = validate_entry({
        "url": URL,
        "maintenance": [{"days": [6, 0, 0], "start": "02:00", "end": "04:00"}],
    })
    assert entry["maintenance"] == [
        {"start": "02:00", "end": "04:00", "days": [0, 6]}
    ], "days are deduplicated and sorted"


def test_no_maintenance_key_is_the_default() -> None:
    assert "maintenance" not in validate_entry({"url": URL})


def test_a_window_survives_the_store(tmp_path: Path) -> None:
    store = tmp_path / "targets.json"
    targetstore.write_store([{"url": URL, "maintenance": [NIGHT]}], store)
    assert targetstore.read_store(store)[0]["maintenance"] == [NIGHT]


# =============================================================================
# C. The dashboard has to say so
# =============================================================================


def _state(status: str = "DOWN") -> dict:
    return {"status": status, "last_checked": "2026-08-01T00:00:00Z",
            "last_error": "boom"}


def test_a_silenced_target_is_marked_on_the_page() -> None:
    """A muted target can be DOWN and say nothing, so a row identical to a
    healthy one is worse than no row: the reader sees a status and assumes
    somebody would have been told."""
    page = dashboard.render_page(
        {URL: _state()}, {}, 10, None, {URL: NIGHT})
    assert '<span class="muted-badge"' in page
    assert "02:00" in page and "04:00" in page, "the window must be named"


def test_an_unsilenced_target_carries_no_badge() -> None:
    page = dashboard.render_page({URL: _state()}, {}, 10)
    assert '<span class="muted-badge"' not in page


def test_a_hostile_window_cannot_inject_markup() -> None:
    """The store validates the format, but the renderer must not depend on
    that -- the same structural rule every other value on this page follows."""
    page = dashboard.render_page(
        {URL: _state()}, {}, 10, None,
        {URL: {"start": "<script>x</script>", "end": "04:00"}})
    assert "<script>x" not in page
    assert "&lt;script&gt;" in page


# =============================================================================
# D. The two properties the whole feature rests on
# =============================================================================

import asyncio
import json

import main
from notifiers import AlertKind


class _Recorder:
    """Stands in for dispatch_alert and records whether it was called."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, session, url, status_detail, kind):
        self.calls += 1
        return True


async def test_a_window_silences_the_alert(monkeypatch) -> None:
    spy = _Recorder()
    monkeypatch.setattr(main, "dispatch_alert", spy)
    monkeypatch.setattr(main, "in_maintenance", lambda w, n: NIGHT)

    await main._alert_unless_muted(None, URL, "boom", AlertKind.FAILURE, [NIGHT])
    assert spy.calls == 0, "nothing should have been dispatched"


async def test_outside_the_window_the_alert_goes_out(monkeypatch) -> None:
    spy = _Recorder()
    monkeypatch.setattr(main, "dispatch_alert", spy)
    monkeypatch.setattr(main, "in_maintenance", lambda w, n: None)

    await main._alert_unless_muted(None, URL, "boom", AlertKind.FAILURE, [NIGHT])
    assert spy.calls == 1


async def test_a_muted_alert_does_not_arm_the_retry(monkeypatch) -> None:
    """The property that keeps planned quiet from becoming a storm.

    Fase 23 re-sends anything that reached nobody. If a deliberate silence
    counted as a failed delivery, every alert suppressed during a window would
    queue up and fire the instant it closed -- turning the feature into the
    opposite of itself.
    """
    monkeypatch.setattr(main, "in_maintenance", lambda w, n: NIGHT)
    owed_nothing = await main._alert_unless_muted(
        None, URL, "boom", AlertKind.FAILURE, [NIGHT])
    assert owed_nothing is True, "muted means nothing is owed, so no retry"


async def test_the_probe_keeps_measuring_during_a_window(tmp_path, monkeypatch) -> None:
    """The load-bearing decision of this phase, asserted rather than promised.

    A window that stopped probing would leave a gap on the dashboard
    indistinguishable from "the monitor died", and would make uptime lie about
    a period nobody measured. Trading a known noise for an unknown silence is
    the worse deal every time.
    """
    monkeypatch.setattr(main, "in_maintenance", lambda w, n: NIGHT)
    spy = _Recorder()
    monkeypatch.setattr(main, "dispatch_alert", spy)

    state_file = tmp_path / "state.json"
    await main.run_health_checks(
        targets=[main.Target(url=URL, maintenance=[NIGHT])],
        state_path=state_file,
        history_path=tmp_path / "history.db",
    )

    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert URL in stored["targets"], "the target was still probed and recorded"
    assert stored["targets"][URL].get("last_checked"), "with a real timestamp"
    assert spy.calls == 0, "and nobody was alerted"


# =============================================================================
# E. Regressions from the audit of this phase
# =============================================================================


def test_a_window_configured_in_the_store_reaches_the_target(tmp_path) -> None:
    """The audit's HIGH, as an assertion.

    Everything about this feature worked in isolation -- the store validated
    and persisted the window, in_maintenance evaluated it correctly, the funnel
    honoured it, and the dashboard drew the badge (it reads the store
    directly). And it did nothing in production, because load_targets built
    Target objects without the field. Two subsystems disagreed about reality
    and only the visible one was right: the operator read "silenced" and got
    paged anyway.

    Every one of the 491 tests passed while the feature was inert, because all
    of them construct Target by hand. This one goes through load_targets.
    """
    store = tmp_path / "targets.json"
    yaml  = tmp_path / "targets.yaml"
    yaml.write_text(f"targets:\n  - {URL}\n", encoding="utf-8")
    targetstore.write_store(
        [validate_entry({"url": URL, "maintenance": [NIGHT], "remind": True})], store)

    target = main.load_targets(yaml, store_path=store)[0]
    assert target.maintenance == [NIGHT], (
        "a window configured from the dashboard must reach the object "
        "check_url actually receives"
    )
    assert target.remind is True, "and so must every other per-target field"


def test_the_documented_fullday_recipe_covers_the_whole_day() -> None:
    """The store rejects start == end and tells the operator to use
    "00:00 a 23:59" instead. With an exclusive end that recipe left the last
    minute of every day unsilenced -- the message and the implementation
    disagreeing once a day, forever."""
    fullday = [{"start": "00:00", "end": "23:59"}]
    assert in_maintenance(fullday, _at(3, 23, 59)), "23:59 is inside a full day"
    assert in_maintenance(fullday, _at(3, 0, 0)),   "and so is midnight"


def test_an_ordinary_end_stays_exclusive() -> None:
    """Only the documented full-day end is special-cased. Making every end
    inclusive would overlap two adjacent windows and silence a minute nobody
    asked to silence."""
    assert not in_maintenance([NIGHT], _at(3, 4, 0)), "04:00 ends the window"


async def test_a_mute_does_not_erase_an_undelivered_alert(monkeypatch, tmp_path) -> None:
    """The audit's second HIGH.

    A failed delivery arms alert_pending -- the only memory that nobody was
    ever told. A window opening on the RETRY used to clear it, and since the
    status had not changed, no later transition would ever fire: the outage
    stayed buried after the window closed. Silencing a NEW alert is the
    feature; erasing the record of an OLD one that never arrived is data loss.
    """
    state = main.StateManager(tmp_path / "state.json")
    await state.set_down(URL, "boom")
    await state.record_alert_outcome(URL, delivered=False)   # every channel down
    assert state._state[URL]["alert_pending"] is True

    monkeypatch.setattr(main, "in_maintenance", lambda w, n: NIGHT)
    spy = _Recorder()
    monkeypatch.setattr(main, "dispatch_alert", spy)
    await main._alert_unless_muted(
        None, URL, "boom", AlertKind.FAILURE, [NIGHT], state)

    assert spy.calls == 0, "muted: nothing was sent"
    assert state._state[URL]["alert_pending"] is True, (
        "and the retry armed by a real delivery failure survives the mute"
    )


async def test_a_delivered_alert_still_clears_the_flag(monkeypatch, tmp_path) -> None:
    """The other half. Moving the bookkeeping into the funnel must not stop it
    happening -- otherwise every delivered alert would re-send forever."""
    state = main.StateManager(tmp_path / "state.json")
    await state.set_down(URL, "boom")
    await state.record_alert_outcome(URL, delivered=False)

    monkeypatch.setattr(main, "in_maintenance", lambda w, n: None)
    monkeypatch.setattr(main, "dispatch_alert", _Recorder())
    await main._alert_unless_muted(
        None, URL, "boom", AlertKind.FAILURE, None, state)

    assert state._state[URL]["alert_pending"] is False


def test_the_compose_file_holds_no_literal_secret() -> None:
    """A hardcoded SECRET_KEY is byte-identical in every clone of a public
    repo, and Django signs session cookies with it. Loopback-only today, but
    publishing the port is a one-line change nobody would pair with rotating a
    key that already works."""
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml"
               ).read_text(encoding="utf-8")
    assert "SECRET_KEY=${WATCHDOG_SECRET_KEY" in compose
    assert "vigil-sre-local-demo" not in compose
