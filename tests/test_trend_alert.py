"""
The trend ALERT — the channel the trend verdict never had.

latency_trend has computed degrading/improving verdicts since trend detection
shipped, and for three releases the only consumer was a --report section, which
means the signal reached a human only if that human already suspected something
and ran a command by hand. These tests cover the part that was missing, and
they are written as outcome tests on purpose.

This project has shipped an inert feature twice: the per-target backend
threshold was completely dead with 491 tests passing because load_targets
discarded the field, and removing
`backend_slack_ms=` from the analyze() call left the whole suite green. Both
escaped because the tests asserted that a value was PRESENT rather than that it
CHANGED THE OUTCOME. Every test below asserts an outcome: an alert was sent, or
it was not.
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import main
from diagnostics import ProbePhases, TREND_MIN_CHANGE_PCT, TREND_MIN_SAMPLES
from history import HistoryRecorder
from notifiers import AlertKind, DiscordNotifier, SlackNotifier

URL = "https://drifting.example"
N   = TREND_MIN_SAMPLES + 10


def _seed(db: Path, url: str, days_ago: float, ttfb: float, count: int) -> None:
    now = datetime.now(timezone.utc)
    outcomes = [
        main.CheckOutcome(
            url=url, status="UP",
            checked_at=(now - timedelta(days=days_ago, minutes=i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            phases=ProbePhases(url=url, http_status=200, ttfb_ms=ttfb + (i % 3),
                               transfer_ms=1.0, body_bytes=100),
        )
        for i in range(count)
    ]
    asyncio.run(HistoryRecorder(db).record_run(
        now.strftime("%Y-%m-%dT%H:%M:%SZ"), outcomes))


def _degrading(tmp_path: Path) -> Path:
    """History where the recent window is ~660% slower than the previous one."""
    db = tmp_path / "history.db"
    _seed(db, URL, days_ago=10, ttfb=50.0,  count=N)
    _seed(db, URL, days_ago=2,  ttfb=380.0, count=N)
    return db


def _steady(tmp_path: Path) -> Path:
    db = tmp_path / "steady.db"
    _seed(db, URL, days_ago=10, ttfb=200.0, count=N)
    _seed(db, URL, days_ago=2,  ttfb=200.0, count=N)
    return db


def _state(tmp_path: Path, name: str = "state.json") -> "main.StateManager":
    """A StateManager with a record for URL already created.

    record_trend_alert is a deliberate no-op when the record does not exist, so
    a test that skipped this would pass on suppression logic that never wrote
    anything.
    """
    state = main.StateManager(tmp_path / name)
    asyncio.run(state.set_up(URL))
    return state


class _Spy:
    """Stands in for _alert_unless_muted and remembers what it was asked to send.

    Returns a real AlertOutcome, because the caller now branches on it: the
    suppression marker is only written when the alert actually reached a
    channel. A spy returning None would make every test look like a delivery
    failure and hide the behaviour it is supposed to be checking.
    """

    def __init__(self, outcome: main.AlertOutcome = None) -> None:  # type: ignore[assignment]
        self.sent: list[tuple[str, AlertKind, str]] = []
        self.outcome = outcome or main.AlertOutcome.DELIVERED

    async def __call__(self, session, url, detail, kind,
                       maintenance=None, state=None) -> "main.AlertOutcome":
        self.sent.append((url, kind, detail))
        return self.outcome


# =============================================================================
# A. The alert fires, and says something useful
# =============================================================================


def test_a_degrading_target_actually_dispatches_an_alert(tmp_path, monkeypatch):
    """The verdict existed for three releases and reached nobody. This is the
    assertion that the channel is connected."""
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state = _state(tmp_path)

    sent = asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL)], state, _degrading(tmp_path)))

    assert sent == 1
    assert len(spy.sent) == 1
    url, kind, detail = spy.sent[0]
    assert url == URL
    assert kind is AlertKind.TREND_DEGRADING
    # The 1 AM test: the message has to carry the numbers, or the person paged
    # has to go query the database to find out what changed. Asserted as a
    # shape, not as literals -- the fixture jitters ttfb by (i % 3), so pinning
    # "50" and "380" would be testing the fixture's arithmetic instead of the
    # message, and would break on any change to the seed.
    shape = re.fullmatch(
        r"p95 (\d+)ms → (\d+)ms \(\+(\d+)%, \+(\d+)ms\) over 7d vs the 7d before",
        detail,
    )
    assert shape, f"the alert detail carries no numbers: {detail!r}"
    before, after, pct, _ = (int(g) for g in shape.groups())
    assert after > before, "reported a degradation that reads as an improvement"
    assert pct >= TREND_MIN_CHANGE_PCT


def test_a_steady_target_says_nothing(tmp_path, monkeypatch):
    """A signal that fires on a target that did not change is one an operator
    learns to ignore."""
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state = _state(tmp_path)

    sent = asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL)], state, _steady(tmp_path)))

    assert sent == 0
    assert spy.sent == []


# =============================================================================
# B. trend_alerts=False — the httpbin decision, as an outcome
# =============================================================================


def test_trend_alerts_false_suppresses_the_alert(tmp_path, monkeypatch):
    """That the flag REACHES Target does not prove _maybe_alert_trend USES it.

    This is the exact shape of the inert per-target backend threshold. The
    history here is unambiguously degrading, so if the flag were read and ignored -- or
    accepted by the YAML and dropped on the way -- this test is what fails.
    """
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state = _state(tmp_path)

    sent = asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL, trend_alerts=False)], state, _degrading(tmp_path)))

    assert sent == 0
    assert spy.sent == [], "trend_alerts=False did not suppress the alert"


def test_suppression_does_not_leak_to_other_targets(tmp_path, monkeypatch):
    """One silenced target must not silence the list. httpbin is off; the
    targets that share the file are not."""
    other = "https://other.example"
    db    = _degrading(tmp_path)
    _seed(db, other, days_ago=10, ttfb=50.0,  count=N)
    _seed(db, other, days_ago=2,  ttfb=380.0, count=N)

    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state = main.StateManager(tmp_path / "state.json")
    asyncio.run(state.set_up(URL))
    asyncio.run(state.set_up(other))

    sent = asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL, trend_alerts=False), main.Target(url=other)],
        state, db))

    assert sent == 1
    assert [u for u, _, _ in spy.sent] == [other]


# =============================================================================
# C. Once per episode, not once per run
# =============================================================================


def test_the_same_episode_does_not_re_alert_on_the_next_run(tmp_path, monkeypatch):
    """A 7-day window compared hourly shares ~99% of its data with the
    comparison an hour earlier, so the verdict does not change. Without
    suppression this sends the same sentence every hour until the drift ages
    out -- which is how a real signal gets muted by its recipient."""
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state = _state(tmp_path)
    db    = _degrading(tmp_path)
    targets = [main.Target(url=URL)]

    first  = asyncio.run(main._maybe_alert_trend(targets, state, db))
    second = asyncio.run(main._maybe_alert_trend(targets, state, db))
    third  = asyncio.run(main._maybe_alert_trend(targets, state, db))

    assert first == 1
    assert (second, third) == (0, 0), "re-alerted on an unchanged verdict"
    assert len(spy.sent) == 1


def test_a_locked_database_skips_the_check_instead_of_killing_the_run(
        tmp_path, monkeypatch):
    """The trend is advisory. The probing it advises on is not.

    latency_percentiles swallows only the missing-schema OperationalError and
    re-raises the rest, `database is locked` included. Unguarded, that took the
    whole cycle down before state.prune() and before the exit code -- and under
    compose `python main.py || exit 1` turns that into a container crash-loop
    for as long as the lock is held.
    """
    import sqlite3

    def locked(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main, "latency_trend_windows", locked)
    monkeypatch.setattr(main, "_alert_unless_muted", _Spy())
    state = _state(tmp_path)

    delivered = asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL)], state, _degrading(tmp_path)))

    assert delivered == 0, "a locked database must not take the run down"


def test_a_target_with_no_state_record_is_skipped_not_spammed(tmp_path, monkeypatch):
    """record_trend_alert is a no-op for an unknown url, by design.

    The suppression read returns None for exactly the same reason, so the alert
    used to fire every cycle and never remember -- up to 1,440 messages a day
    for one p95. Reachable when state.json is lost while history.db survives,
    which compose makes easy by putting them on separate volumes.
    """
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state   = main.StateManager(tmp_path / "state.json")   # no set_up: no record
    db      = _degrading(tmp_path)
    targets = [main.Target(url=URL)]

    for _ in range(3):
        asyncio.run(main._maybe_alert_trend(targets, state, db))

    assert spy.sent == [], (
        "alerted with nowhere to persist the marker, so every cycle repeats it"
    )


def test_a_muted_alert_does_not_count_as_communicated(tmp_path, monkeypatch):
    """A maintenance window must not consume the episode.

    The marker used to be written before dispatch, so a window active at the
    moment the p95 crossed made the marker claim an alert nobody received --
    and the marker then suppressed that episode forever, including long after
    the window closed. A weekend freeze silenced a real degradation for good.
    """
    spy = _Spy(main.AlertOutcome.MUTED)
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state   = _state(tmp_path)
    db      = _degrading(tmp_path)
    targets = [main.Target(url=URL)]

    delivered = asyncio.run(main._maybe_alert_trend(targets, state, db))

    assert delivered == 0, "a muted alert reached nobody, so nothing was delivered"
    assert state.trend_alerted_p95(URL) is None, (
        "a muted alert was recorded as communicated — the episode is now lost"
    )

    # The window closes and the degradation is unchanged: it must be announced.
    open_spy = _Spy(main.AlertOutcome.DELIVERED)
    monkeypatch.setattr(main, "_alert_unless_muted", open_spy)
    assert asyncio.run(main._maybe_alert_trend(targets, state, db)) == 1
    assert state.trend_alerted_p95(URL) is not None


def test_an_undelivered_alert_is_retried_next_cycle(tmp_path, monkeypatch):
    """A rotated webhook must not consume the episode either.

    Same marker, same failure: recording before dispatch meant every channel
    could be down and the system would still behave as though it had warned
    somebody, with undelivered_alerts() reporting 0 and the exit code clean.
    """
    spy = _Spy(main.AlertOutcome.UNDELIVERED)
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state   = _state(tmp_path)
    db      = _degrading(tmp_path)
    targets = [main.Target(url=URL)]

    assert asyncio.run(main._maybe_alert_trend(targets, state, db)) == 0
    assert state.trend_alerted_p95(URL) is None

    asyncio.run(main._maybe_alert_trend(targets, state, db))
    assert len(spy.sent) == 2, "an alert that reached nobody was never retried"


def test_the_funnel_is_told_about_state_so_a_lost_alert_is_visible(tmp_path, monkeypatch):
    """The trend call site used to omit `state`, which is what arms the retry.

    Without it, alert_pending is never set, undelivered_alerts() counts 0, and
    the exit code stays 0 — the one alert kind with no operational trace at all
    when delivery fails.
    """
    seen: dict = {}

    async def capture(session, url, detail, kind, maintenance=None, state=None):
        seen["state"] = state
        return main.AlertOutcome.DELIVERED

    monkeypatch.setattr(main, "_alert_unless_muted", capture)
    state = _state(tmp_path)
    asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL)], state, _degrading(tmp_path)))

    assert seen["state"] is state, "the funnel cannot arm the retry without state"


def test_the_marker_survives_the_status_writer(tmp_path, monkeypatch):
    """The gap every other test in this file missed.

    The tests above call _maybe_alert_trend twice in a row. A real cycle does
    not: it probes first, and the probe's status write REPLACES the record.
    Anything not listed in _CARRY_FORWARD_FIELDS is dropped there, so the
    marker was wiped and rewritten every cycle and the alert re-sent every
    cycle -- the storm the marker exists to prevent, with the whole suite
    green. It was caught by running the container twice against the same state
    and history, which is what this test now does without Docker.
    """
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state   = _state(tmp_path)
    db      = _degrading(tmp_path)
    targets = [main.Target(url=URL)]

    asyncio.run(main._maybe_alert_trend(targets, state, db))
    assert len(spy.sent) == 1

    # What the next cycle does before it gets anywhere near the trend.
    asyncio.run(state.set_up(URL))
    assert state.trend_alerted_p95(URL) is not None, (
        "the status write dropped the marker — every cycle will re-alert"
    )

    asyncio.run(main._maybe_alert_trend(targets, state, db))
    assert len(spy.sent) == 1, "re-alerted after a status write"


def test_a_much_worse_episode_alerts_again(tmp_path, monkeypatch):
    """Once per episode, but "the same episode" is not "the same magnitude".

    A backend drifting 400 -> 900 -> 2000 -> 5000 ms over a month never returns
    a non-degrading verdict, so the marker never clears and the only alert ever
    sent is the first — the smallest problem of the month. Another 50% on top of
    what was already announced is worth saying out loud.
    """
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state   = _state(tmp_path)
    targets = [main.Target(url=URL)]

    asyncio.run(main._maybe_alert_trend(targets, state, _degrading(tmp_path)))
    first = state.trend_alerted_p95(URL)
    assert len(spy.sent) == 1 and first is not None

    # Slightly worse, well under another 50%: still the same episode.
    mild = tmp_path / "mild.db"
    _seed(mild, URL, days_ago=10, ttfb=50.0,  count=N)
    _seed(mild, URL, days_ago=2,  ttfb=first * 1.1, count=N)
    asyncio.run(main._maybe_alert_trend(targets, state, mild))
    assert len(spy.sent) == 1, "re-alerted on a change below the floor"

    # Far worse: owed another alert, and the marker moves up with it.
    worse = tmp_path / "worse.db"
    _seed(worse, URL, days_ago=10, ttfb=50.0,  count=N)
    _seed(worse, URL, days_ago=2,  ttfb=first * 2.0, count=N)
    asyncio.run(main._maybe_alert_trend(targets, state, worse))
    assert len(spy.sent) == 2, "a doubling after the first alert said nothing"
    assert state.trend_alerted_p95(URL) > first


def test_the_marker_is_persisted_not_just_held_in_memory(tmp_path, monkeypatch):
    """Suppression that lives in a variable is suppression that dies with the
    process, and this process exits after every cycle."""
    monkeypatch.setattr(main, "_alert_unless_muted", _Spy())
    state = _state(tmp_path)
    asyncio.run(main._maybe_alert_trend(
        [main.Target(url=URL)], state, _degrading(tmp_path)))

    reloaded = main.StateManager(tmp_path / "state.json")
    assert reloaded.trend_alerted_p95(URL) is not None


def test_a_recovered_trend_arms_the_next_episode(tmp_path, monkeypatch):
    """The certificate alert shipped with exactly this bug: a suppression
    marker that nothing cleared stayed set through the next full decay. A
    target that alerted once and then recovered has to be able to alert again."""
    spy = _Spy()
    monkeypatch.setattr(main, "_alert_unless_muted", spy)
    state   = _state(tmp_path)
    targets = [main.Target(url=URL)]

    asyncio.run(main._maybe_alert_trend(targets, state, _degrading(tmp_path)))
    assert state.trend_alerted_p95(URL) is not None

    # Recovered: the verdict is no longer degrading, so the marker must clear.
    asyncio.run(main._maybe_alert_trend(targets, state, _steady(tmp_path)))
    assert state.trend_alerted_p95(URL) is None, "nothing cleared the marker"

    # And the next real episode is heard.
    asyncio.run(main._maybe_alert_trend(targets, state, _degrading(tmp_path)))
    assert len(spy.sent) == 2


def test_an_unmeasurable_window_clears_the_marker_too(tmp_path, monkeypatch):
    """no_data and too_few_samples are not "still degrading". A target that
    stopped producing comparable windows has stopped being measurable, and
    holding the marker would swallow the alert for the next real drift."""
    monkeypatch.setattr(main, "_alert_unless_muted", _Spy())
    state   = _state(tmp_path)
    targets = [main.Target(url=URL)]

    asyncio.run(main._maybe_alert_trend(targets, state, _degrading(tmp_path)))
    assert state.trend_alerted_p95(URL) is not None

    asyncio.run(main._maybe_alert_trend(targets, state, tmp_path / "empty.db"))
    assert state.trend_alerted_p95(URL) is None


# =============================================================================
# D. The config path, and the channels
# =============================================================================


def test_the_yaml_flag_survives_validation(tmp_path):
    """A field the validator strips is a field the operator set and the system
    ignored -- silently, which is the worst of the three outcomes."""
    import targetstore

    entry = targetstore.validate_entry(
        {"url": URL, "trend_alerts": False})
    assert entry["trend_alerts"] is False

    assert targetstore.validate_entry({"url": URL})["trend_alerts"] is True


def test_a_non_boolean_flag_is_refused(tmp_path):
    """'false' as a string is the classic YAML trap, and it is truthy."""
    import pytest
    import targetstore

    with pytest.raises(targetstore.ValidationError):
        targetstore.validate_entry({"url": URL, "trend_alerts": "false"})


def test_both_channels_render_the_new_kind(tmp_path):
    """Both payload builders end in `raise ValueError` for an unhandled kind.
    That is the right design and it means a new AlertKind wired into one
    channel and not the other fails at dispatch, in front of the person who
    needed the alert."""
    for cls in (DiscordNotifier, SlackNotifier):
        payload = cls().build_payload(
            URL, "p95 50ms → 380ms (+660%, +330ms)",
            "2026-09-14T12:00:00Z", AlertKind.TREND_DEGRADING,
        )
        assert payload, f"{cls.__name__} produced no payload"
