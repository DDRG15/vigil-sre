"""
tests/test_maintenance_close.py — an outage that outlives its maintenance window.

Why this exists
---------------
A maintenance window silences the ALERT, never the probe. A target that goes
DOWN inside its window therefore has its FAILURE muted, by design. If the
target is still DOWN when the window closes, nothing announces it: the status
transition already happened (while muted), every later run reads "DOWN -> DOWN,
already alerted", and with `remind` off no reminder ever comes. Confirmed
2026-10-04: 0 alerts, forever, for a target that stayed down.

The fix is a marker on the target's record, `muted_alert_owed`: a FAILURE or
DEGRADED transition was muted and the current state has not been announced.
The first run after the window announces the CURRENT state once and clears it.

Constraints these tests hold in place:
  - A muted alert still does not arm the retry (alert_pending). Queueing the
    suppressed alerts would fire them all when the window closes.
  - The marker is cleared only after a delivery is confirmed -- never before.
  - A recovery that happened during the window is not announced afterwards.
  - An announcement that reaches nobody is an ordinary lost status alert:
    alert_pending takes over and retries it.
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
from diagnostics import ProbePhases

URL   = "https://svc.example"
NIGHT = {"start": "02:00", "end": "04:00"}
FIELD = "muted_alert_owed"


class _World:
    """The clock-and-network the monitor sees: is the window open, does the
    target answer, does any channel deliver."""

    def __init__(self) -> None:
        self.window    = False
        self.probe     = "ok"           # "ok" | "slow" | "fail"
        self.delivers  = True
        self.kinds: list[str] = []
        self.state: main.StateManager | None = None
        self.marker_at_dispatch: list[bool] = []

    async def dispatch(self, session, url, status_detail, kind) -> bool:
        self.kinds.append(kind.name)
        if self.state is not None:
            self.marker_at_dispatch.append(
                bool(self.state._state[URL].get(FIELD)))
        return self.delivers

    def in_maintenance(self, windows, now):
        return NIGHT if self.window else None

    async def probe_impl(self, *a, **k):
        if self.probe == "fail":
            raise main._ProbeFailure("HTTP 503 (expected 200)")
        ttfb = 5000.0 if self.probe == "slow" else 50.0
        return ProbePhases(
            url=URL, http_status=200, ttfb_ms=ttfb, transfer_ms=1.0,
            body_bytes=10, tls_cert_days_left=None,
        )

    def take(self) -> list[str]:
        """The alerts sent since the last call."""
        taken, self.kinds = self.kinds, []
        return taken


@pytest.fixture
def world(monkeypatch) -> _World:
    w = _World()
    monkeypatch.setattr(main, "dispatch_alert", w.dispatch)
    monkeypatch.setattr(main, "in_maintenance", w.in_maintenance)
    monkeypatch.setattr(main, "_probe_with_backoff", w.probe_impl)

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    return w


@pytest.fixture
def state(tmp_path, world, request, monkeypatch) -> main.StateManager:
    if "real_writes" not in request.fixturenames:
        # Same reason as test_alert_kinds: nothing here reads the file, and on
        # Windows every atomic rename costs real time.
        monkeypatch.setattr(main.StateManager, "_write_sync", lambda self: None)
    sm = main.StateManager(tmp_path / "state.json")
    world.state = sm
    return sm


async def _round(sm: main.StateManager, **kw) -> main.CheckOutcome:
    return await main.check_url(URL, None, sm, maintenance=[NIGHT], **kw)


async def _rounds(sm: main.StateManager, n: int, **kw) -> None:
    for _ in range(n):
        await _round(sm, **kw)


async def _healthy_and_told(sm: main.StateManager) -> None:
    await sm.set_up(URL)
    await sm.record_alert_outcome(URL, True)


# =============================================================================
# A. The defect: DOWN inside the window, still DOWN after it
# =============================================================================


async def test_an_outage_that_outlives_the_window_is_announced_once(world, state) -> None:
    await _healthy_and_told(state)

    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS + 3)
    assert state.current_status(URL) == main.STATUS_DOWN
    assert world.take() == [], "inside the window: nothing is sent"

    world.window = False
    await _round(state)
    assert world.take() == ["FAILURE"], "first run after the window announces it"

    await _rounds(state, 3)
    assert world.take() == [], "and then it is quiet: once per transition"


async def test_a_muted_alert_still_does_not_arm_the_retry(world, state) -> None:
    """The rule the maintenance suite already holds, held again at this seam:
    owing an announcement is a different thing from a failed delivery."""
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS + 1)

    assert not state._state[URL].get("alert_pending")
    assert state._state[URL].get(FIELD) is True, "the owed announcement is recorded"


async def test_a_degraded_outlasting_the_window_is_announced_as_degraded(world, state) -> None:
    await _healthy_and_told(state)

    world.window, world.probe = True, "slow"
    await _rounds(state, 3)
    assert state.current_status(URL) == main.STATUS_DEGRADED
    assert world.take() == []

    world.window = False
    await _round(state)
    assert world.take() == ["DEGRADED"]
    await _rounds(state, 2)
    assert world.take() == []


async def test_a_degraded_that_became_down_in_the_window_is_announced_as_down(
    world, state
) -> None:
    """The announcement is the kind of the state the target is in NOW."""
    await _healthy_and_told(state)
    world.window, world.probe = True, "slow"
    await _round(state)
    world.probe = "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS)
    assert state.current_status(URL) == main.STATUS_DOWN

    world.window = False
    await _round(state)
    assert world.take() == ["FAILURE"]


async def test_a_first_sighting_that_is_down_inside_the_window_is_announced_after(
    world, state
) -> None:
    """Fresh install, window open: unknown -> DOWN is muted like any other."""
    world.window, world.probe = True, "fail"
    await _rounds(state, 2)
    assert world.take() == []

    world.window = False
    await _round(state)
    assert world.take() == ["FAILURE"]


# =============================================================================
# B. What must stay quiet
# =============================================================================


async def test_an_outage_that_recovered_inside_the_window_is_never_announced(
    world, state
) -> None:
    """The muted RECOVERY is dropped, as the window always did: planned quiet
    covers both edges. What this fix must not do is announce an outage that is
    over -- 'DOWN' after the window for a target that is UP is a false page."""
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS)

    world.probe = "ok"
    await _round(state)
    assert FIELD not in state._state[URL], "recovery retires the owed announcement"

    world.window = False
    await _rounds(state, 3)
    assert world.take() == []


async def test_the_marker_does_not_leak_into_the_next_outage(world, state) -> None:
    """After a window-muted outage recovered, a LATER outage outside any window
    alerts exactly once, normally."""
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS)
    world.probe = "ok"
    await _round(state)
    world.window = False

    world.probe = "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS + 2)
    assert world.take() == ["FAILURE"]
    assert FIELD not in state._state[URL]


async def test_an_alert_delivered_outside_a_window_never_sets_the_marker(world, state) -> None:
    await _healthy_and_told(state)
    world.probe = "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS)

    assert world.take() == ["FAILURE"]
    assert FIELD not in state._state[URL]


async def test_a_target_that_is_up_when_the_window_closes_sends_nothing(world, state) -> None:
    await _healthy_and_told(state)
    world.window = True
    await _rounds(state, 2)
    world.window = False
    await _rounds(state, 2)
    assert world.take() == []


# =============================================================================
# C. Delivery bookkeeping: never mark before confirming
# =============================================================================


async def test_the_marker_is_still_set_while_the_announcement_is_in_flight(
    world, state
) -> None:
    """Mark-before-confirm, as an assertion: at the moment the alert is being
    dispatched the owed-marker must still be there. Cleared up front, a crash
    between the clear and the delivery loses the announcement for good."""
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS)
    world.window = False
    world.marker_at_dispatch.clear()

    await _round(state)

    assert world.marker_at_dispatch == [True]
    assert FIELD not in state._state[URL], "cleared once delivery was confirmed"


async def test_an_announcement_lost_on_every_channel_becomes_an_ordinary_retry(
    world, state
) -> None:
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS)
    world.window = False

    world.delivers = False
    await _round(state)
    assert world.take() == ["FAILURE"]
    assert state._state[URL]["alert_pending"] is True, "lost like any status alert"

    world.delivers = True
    await _round(state)
    assert world.take() == ["FAILURE"], "the retry delivers it"
    assert not state._state[URL].get("alert_pending")
    assert FIELD not in state._state[URL]

    await _rounds(state, 2)
    assert world.take() == [], "and then quiet"


# =============================================================================
# D. Interaction with reminders
# =============================================================================


async def test_a_reminder_that_follows_the_window_is_the_one_announcement(
    world, state
) -> None:
    """remind=True and the outage is hours old when the window closes. The
    STILL_DOWN reminder tells the on-call everything the FAILURE would, with
    the duration. Sending both in one run is a double page for one event."""
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS, remind=True)
    state._state[URL]["last_changed"] = (
        datetime.now(timezone.utc) - timedelta(hours=4)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert world.take() == []

    world.window = False
    await _round(state, remind=True)
    assert world.take() == ["STILL_DOWN"]
    assert FIELD not in state._state[URL]

    await _round(state, remind=True)
    assert world.take() == [], "no FAILURE trailing the reminder"


# =============================================================================
# E. It has to survive the process: one run = one process
# =============================================================================


@pytest.fixture
def real_writes() -> None:
    """Opt-in marker: the test reads state.json back, so writes must hit disk."""


async def test_the_owed_announcement_survives_a_restart(
    world, state, real_writes, tmp_path
) -> None:
    """Each run is a new process. A marker that only lives in memory, or that a
    status rewrite drops, never reaches the run after the window closes."""
    path = tmp_path / "state.json"
    await _healthy_and_told(state)
    world.window, world.probe = True, "fail"
    await _rounds(state, main.DOWN_CONFIRMATIONS + 1)

    for _ in range(2):                              # two more "processes", still muted
        again = main.StateManager(path)
        world.state = again
        await _round(again)
    on_disk = json.loads(path.read_text(encoding="utf-8"))["targets"][URL]
    assert on_disk[FIELD] is True, "the marker is in the file, not only in memory"
    assert world.take() == []

    world.window = False
    fresh = main.StateManager(path)
    world.state = fresh
    await _round(fresh)
    assert world.take() == ["FAILURE"]

    last = main.StateManager(path)
    world.state = last
    await _round(last)
    assert world.take() == []
    assert FIELD not in json.loads(path.read_text(encoding="utf-8"))["targets"][URL]
