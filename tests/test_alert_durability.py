"""
tests/test_alert_durability.py -- a status alert that was decided must survive
the process dying before it was sent.

The order used to be: the setter persists the transition, the funnel dispatches
(up to ~75 s of webhook retries), and only afterwards record_alert_outcome wrote
alert_pending. A SIGKILL, an OOM kill or a host reboot inside that window left
state.json saying "DOWN, nothing owed". The next round read "already DOWN, no
change" and the FAILURE was never sent, by anyone, ever -- the on-call learns of
the outage from a customer.

The obligation is therefore written in the SAME atomic write as the transition
(the setter arms alert_pending when it answers "alert needed") and cleared only
once a channel accepted the alert. Delivery is at-least-once: a kill between the
channel accepting and the clear sends a duplicate, which is the right side of
that trade.

Every test builds a fresh StateManager from the file on disk to stand in for the
next process; nothing is read from the instance that "died".

Coverage:
  A. The setters carry the obligation in the transition's own write
  B. A kill between the write and the dispatch -> the next round re-sends
  C. Delivered, all channels failed, muted, recovered before the retry
  D. A retry does not rewrite last_changed
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import ProbePhases

URL   = "https://svc.example"
NIGHT = {"start": "02:00", "end": "04:00"}


class _Killed(BaseException):
    """The process dying mid-dispatch. BaseException so nothing in the code
    under test can swallow it the way it would swallow an Exception."""


class _World:
    """The network the monitor sees: does the target answer, does any channel
    take the alert, is the maintenance window open."""

    def __init__(self) -> None:
        self.probe    = "ok"          # "ok" | "slow" | "fail"
        self.dispatch = "deliver"     # "deliver" | "fail" | "kill"
        self.window   = False
        self.sent: list[str] = []

    async def _dispatch(self, session, url, status_detail, kind) -> bool:
        if self.dispatch == "kill":
            raise _Killed()
        self.sent.append(kind.name)
        return self.dispatch == "deliver"

    async def _probe(self, *a, **k) -> ProbePhases:
        if self.probe == "fail":
            raise main._ProbeFailure("HTTP 503 (expected 200)")
        ttfb = 5000.0 if self.probe == "slow" else 50.0
        return ProbePhases(
            url=URL, http_status=200, ttfb_ms=ttfb, transfer_ms=1.0,
            body_bytes=10, tls_cert_days_left=None,
        )

    def _in_maintenance(self, windows, now):
        return NIGHT if self.window else None

    def take(self) -> list[str]:
        taken, self.sent = self.sent, []
        return taken


@pytest.fixture
def world(monkeypatch) -> _World:
    w = _World()
    monkeypatch.setattr(main, "dispatch_alert", w._dispatch)
    monkeypatch.setattr(main, "in_maintenance", w._in_maintenance)
    monkeypatch.setattr(main, "_probe_with_backoff", w._probe)

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    return w


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "state.json"


def _on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["targets"][URL]


async def _round(path: Path) -> main.CheckOutcome:
    """One monitor process: a fresh StateManager read from disk, one check."""
    return await main.check_url(URL, None, main.StateManager(path), maintenance=[NIGHT])


async def _settled_up(path: Path) -> None:
    """A healthy, already-announced target: the starting point of an outage."""
    await main.StateManager(path).set_up(URL)


async def _down_and_told(world: _World, path: Path) -> None:
    """Healthy, then DOWN with the FAILURE delivered."""
    await _settled_up(path)
    world.probe = "fail"
    for _ in range(main.DOWN_CONFIRMATIONS):
        await _round(path)
    assert world.take() == ["FAILURE"]
    assert not _on_disk(path).get("alert_pending")


# =============================================================================
# A. The setters carry the obligation in the transition's own write
# =============================================================================


async def test_a_first_sighting_down_is_owed_the_moment_it_is_written(path) -> None:
    sm = main.StateManager(path)
    assert await sm.set_down(URL, "HTTP 503") is True

    assert _on_disk(path)["alert_pending"] is True, (
        "the transition is on disk but the obligation to announce it is not")


async def test_a_confirmed_down_is_owed_the_moment_it_is_written(path) -> None:
    sm = main.StateManager(path)
    await sm.set_up(URL)
    for _ in range(main.DOWN_CONFIRMATIONS - 1):
        assert await sm.set_down(URL, "HTTP 503") is False
        assert not _on_disk(path).get("alert_pending"), "hysteresis owes nothing"
    assert await sm.set_down(URL, "HTTP 503") is True

    assert _on_disk(path)["status"] == main.STATUS_DOWN
    assert _on_disk(path)["alert_pending"] is True


async def test_a_recovery_is_owed_the_moment_it_is_written(path) -> None:
    sm = main.StateManager(path)
    await sm.set_down(URL, "boom")
    await sm.record_alert_outcome(URL, delivered=True)
    assert await sm.set_up(URL) is True

    assert _on_disk(path)["status"] == main.STATUS_UP
    assert _on_disk(path)["alert_pending"] is True


@pytest.mark.parametrize("previous", ["none", "up", "down"])
async def test_a_degradation_is_owed_the_moment_it_is_written(path, previous) -> None:
    sm = main.StateManager(path)
    if previous == "up":
        await sm.set_up(URL)
    elif previous == "down":
        await sm.set_down(URL, "boom")
        await sm.record_alert_outcome(URL, delivered=True)
    assert await sm.set_degraded(URL, "slow") is True

    assert _on_disk(path)["status"] == main.STATUS_DEGRADED
    assert _on_disk(path)["alert_pending"] is True


async def test_a_first_healthy_sighting_and_a_steady_state_owe_nothing(path) -> None:
    sm = main.StateManager(path)
    assert await sm.set_up(URL) is False
    assert not _on_disk(path).get("alert_pending")
    assert await sm.set_up(URL) is False
    assert not _on_disk(path).get("alert_pending")


# =============================================================================
# B. A kill between the transition write and the dispatch
# =============================================================================


async def test_a_kill_before_dispatch_still_sends_the_failure_next_round(
        world, path) -> None:
    """The defect. Round 1 persists UP -> DOWN and dies inside dispatch. Round 2
    is a fresh process: before the fix it read 'already DOWN, nothing owed' and
    the FAILURE was never sent."""
    await _settled_up(path)
    world.probe = "fail"
    for _ in range(main.DOWN_CONFIRMATIONS - 1):
        await _round(path)                       # hysteresis: no transition yet

    world.dispatch = "kill"
    with pytest.raises(_Killed):
        await _round(path)                       # DOWN confirmed, then SIGKILL
    assert _on_disk(path)["status"] == main.STATUS_DOWN

    world.dispatch = "deliver"
    await _round(path)                           # next process, state from disk
    assert world.take() == ["FAILURE"], "the outage was never announced"

    await _round(path)
    assert world.take() == [], "delivered once: quiet again"
    assert not _on_disk(path).get("alert_pending")


async def test_a_kill_before_dispatch_still_sends_the_recovery_next_round(
        world, path) -> None:
    await _down_and_told(world, path)

    world.probe, world.dispatch = "ok", "kill"
    with pytest.raises(_Killed):
        await _round(path)
    assert _on_disk(path)["status"] == main.STATUS_UP

    world.dispatch = "deliver"
    await _round(path)
    assert world.take() == ["RECOVERY"]
    await _round(path)
    assert world.take() == []


async def test_a_kill_before_dispatch_still_sends_the_degraded_next_round(
        world, path) -> None:
    await _settled_up(path)

    world.probe, world.dispatch = "slow", "kill"
    for _ in range(main.DEGRADED_CONFIRMATIONS - 1):
        await _round(path)
    with pytest.raises(_Killed):
        await _round(path)

    world.dispatch = "deliver"
    await _round(path)
    assert world.take() == ["DEGRADED"]
    await _round(path)
    assert world.take() == []


async def test_a_kill_on_a_first_sighting_down_is_still_announced(world, path) -> None:
    world.probe, world.dispatch = "fail", "kill"
    with pytest.raises(_Killed):
        await _round(path)

    world.dispatch = "deliver"
    await _round(path)
    assert world.take() == ["FAILURE"]


# =============================================================================
# C. Delivered, all channels failed, muted, recovered before the retry
# =============================================================================


async def test_a_delivered_alert_clears_the_obligation_on_disk(world, path) -> None:
    world.probe = "fail"
    await _round(path)

    assert world.take() == ["FAILURE"]
    assert not _on_disk(path).get("alert_pending"), (
        "delivered, yet the obligation is still on disk: it would re-send forever")


async def test_when_every_channel_fails_the_retry_repeats_until_one_accepts(
        world, path) -> None:
    world.probe, world.dispatch = "fail", "fail"
    await _round(path)
    assert world.take() == ["FAILURE"]
    assert _on_disk(path)["alert_pending"] is True

    await _round(path)
    assert world.take() == ["FAILURE"], "still undelivered: retried"
    assert _on_disk(path)["alert_pending"] is True

    world.dispatch = "deliver"
    await _round(path)
    assert world.take() == ["FAILURE"]
    assert not _on_disk(path).get("alert_pending")
    await _round(path)
    assert world.take() == []


async def test_a_muted_transition_does_not_arm_the_retry(world, path) -> None:
    """Planned quiet stays quiet: muted_alert_owed carries the announcement for
    after the window, and arming alert_pending as well would send it twice."""
    await _settled_up(path)
    world.window, world.probe = True, "fail"
    for _ in range(main.DOWN_CONFIRMATIONS + 2):
        await _round(path)

    assert world.take() == [], "inside the window: nothing is sent"
    stored = _on_disk(path)
    assert stored["status"] == main.STATUS_DOWN
    assert not stored.get("alert_pending"), "a mute must not arm the retry"
    assert stored.get("muted_alert_owed") is True

    world.window = False
    await _round(path)
    assert world.take() == ["FAILURE"], "announced once, after the window"
    await _round(path)
    assert world.take() == []


async def test_a_muted_recovery_and_degradation_do_not_arm_the_retry(
        world, path) -> None:
    await _down_and_told(world, path)
    world.window, world.probe = True, "ok"
    await _round(path)
    assert not _on_disk(path).get("alert_pending")
    assert world.take() == []

    world.probe = "slow"
    await _settled_up(path)
    await _round(path)
    assert not _on_disk(path).get("alert_pending")
    assert world.take() == []


async def test_a_mute_landing_on_an_old_undelivered_alert_keeps_it_owed(
        world, path) -> None:
    """The record that a status alert never arrived is not the mute's to erase:
    the window silences NEW alerts, and the old one is still owed afterwards."""
    world.probe, world.dispatch = "fail", "fail"
    await _round(path)
    assert _on_disk(path)["alert_pending"] is True
    world.take()

    world.window = True
    await _round(path)
    assert world.take() == []
    assert _on_disk(path)["alert_pending"] is True, "the mute cleared an old obligation"

    world.window, world.dispatch = False, "deliver"
    await _round(path)
    assert world.take() == ["FAILURE"]
    assert not _on_disk(path).get("alert_pending")


async def test_recovering_before_the_opening_alert_arrives_announces_the_missed_outage(
        world, path) -> None:
    world.probe, world.dispatch = "fail", "fail"
    await _round(path)                           # FAILURE lost
    assert world.take() == ["FAILURE"]

    world.probe, world.dispatch = "ok", "deliver"
    await _round(path)                           # target is back before any retry
    # Not a RECOVERY: that would close something nobody was told opened. One
    # catch-up notice says the outage happened and ended unseen.
    assert world.take() == ["UNANNOUNCED_OUTAGE"]
    assert not _on_disk(path).get("alert_pending")
    await _round(path)
    assert world.take() == []                    # once, not every round


# =============================================================================
# D. A retry does not rewrite last_changed
# =============================================================================


@pytest.mark.parametrize("probe,kind", [("slow", "DEGRADED"), ("ok", "RECOVERY")])
async def test_a_retry_leaves_last_changed_alone(world, path, probe, kind) -> None:
    """last_changed is when the status changed. A retry of the announcement is
    not a change, and rewriting it moves the 'since' on the dashboard and the
    outage clock the reminders count from."""
    if probe == "ok":
        await main.StateManager(path).set_down(URL, "boom")
        await main.StateManager(path).record_alert_outcome(URL, delivered=True)
    else:
        await _settled_up(path)

    world.probe, world.dispatch = probe, "fail"
    if probe == "slow":
        for _ in range(main.DEGRADED_CONFIRMATIONS - 1):
            await _round(path)
    await _round(path)
    assert world.take() == [kind]
    changed_at = _on_disk(path)["last_changed"]

    # Make "now" differ from the stored second so a rewrite is visible.
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["targets"][URL]["last_changed"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(stored), encoding="utf-8")

    await _round(path)
    assert world.take() == [kind], "the undelivered alert is retried"
    assert _on_disk(path)["last_changed"] == "2020-01-01T00:00:00Z", (
        f"retry rewrote last_changed (was {changed_at})")
