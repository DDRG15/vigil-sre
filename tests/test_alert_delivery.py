"""
tests/test_alert_delivery.py — what happens when an alert reaches nobody.

Why this is its own file
------------------------
Every other test here asks "did the monitor reach the right conclusion about a
target". These ask a different question: when the monitor reached the right
conclusion and then failed to tell anyone, does it notice?

That gap was real and reproduced. `dispatch_alert` returned None, so the state
machine advanced to DOWN, recorded "already alerted", and suppressed every
following run. The outage and the silence about it had the same cause — both
channels being unreachable — so nothing else in the system could catch it.
Total silence is indistinguishable from everything being fine, which is the
one failure mode this whole project exists to prevent.

Coverage:
  A. dispatch_alert's delivery verdict
  B. The retry loop: lost -> retried -> delivered -> quiet
  C. undelivered_alerts() and the exit code that surfaces it
  D. The per-run circuit breaker
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from main import StateManager, _exit_code, undelivered_alerts
from notifiers import (
    CIRCUIT_BREAK_AFTER,
    AlertKind,
    DiscordNotifier,
    dispatch_alert,
    reset_circuits,
)

URL = "https://example.com"


class _Channel(DiscordNotifier):
    """A notifier whose delivery outcome the test decides."""

    def __init__(self, name: str, delivers: bool) -> None:
        self.name      = name
        self._delivers = delivers
        self.calls     = 0

    async def send(self, session, url, status_detail, kind) -> bool:
        self.calls += 1
        return self._delivers


# =============================================================================
# A. dispatch_alert's delivery verdict
# =============================================================================


async def test_dispatch_reports_whether_anyone_was_told() -> None:
    """The return value is the whole fix. It used to be None, which the state
    machine could only read as success."""
    assert await dispatch_alert(
        None, URL, "detail", AlertKind.FAILURE,
        notifiers=[_Channel("Up", True), _Channel("Down", False)],
    ) is True, "one live channel means a human was told"

    assert await dispatch_alert(
        None, URL, "detail", AlertKind.FAILURE,
        notifiers=[_Channel("Down", False), _Channel("AlsoDown", False)],
    ) is False, "every channel failing means nobody was told"


async def test_no_configured_channel_is_also_undelivered() -> None:
    """An alert with nowhere to go is lost exactly like one every channel
    refused, and must be counted the same way."""
    assert await dispatch_alert(
        None, URL, "detail", AlertKind.FAILURE, notifiers=[]
    ) is False


# =============================================================================
# B. The retry loop
# =============================================================================


@pytest.mark.parametrize("setter,args", [
    ("set_down",     ("boom",)),
    ("set_degraded", ("slow",)),
])
async def test_an_undelivered_alert_is_retried_next_run(tmp_path, setter, args) -> None:
    """The self-healing loop, on both alerting paths.

    Run 1 transitions and the alert is lost. Run 2 sees no status change --
    which before this fix meant silence forever.
    """
    sm = StateManager(tmp_path / "state.json")
    assert await getattr(sm, setter)(URL, *args) is True
    await sm.record_alert_outcome(URL, delivered=False)

    assert await getattr(sm, setter)(URL, *args) is True, (
        "same status, but the last alert reached nobody -- must retry"
    )


async def test_a_delivered_alert_stops_being_retried(tmp_path: Path) -> None:
    """The other half. Without it, a channel coming back would re-alert every
    minute for as long as the target stayed down -- the alert fatigue this
    project spent three phases removing."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(URL, "boom")
    await sm.record_alert_outcome(URL, delivered=False)
    assert await sm.set_down(URL, "boom") is True

    await sm.record_alert_outcome(URL, delivered=True)
    assert await sm.set_down(URL, "boom") is False, (
        "delivered -- a repeat failure on an already-DOWN target is not news"
    )


async def test_recovery_is_retried_when_it_was_never_delivered(tmp_path: Path) -> None:
    """A missed RECOVERY has its own cost: the operator keeps believing an
    outage is ongoing after it ended."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(URL)
    await sm.record_alert_outcome(URL, delivered=False)
    assert await sm.set_up(URL) is True


async def test_alert_pending_survives_a_status_rewrite(tmp_path: Path) -> None:
    """set_up/set_degraded/set_down REBUILD the record rather than merging, so
    a field not carried forward is gone within 60 seconds. That is precisely
    what would make a lost alert unrecoverable."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(URL, "boom")
    await sm.record_alert_outcome(URL, delivered=False)
    await sm.set_down(URL, "boom again")

    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert stored["targets"][URL]["alert_pending"] is True
    assert stored["schema_version"] == main.STATE_SCHEMA_VERSION


async def test_recording_an_outcome_for_an_unknown_target_is_harmless(tmp_path) -> None:
    """The alert already happened. Losing the bookkeeping must not also cost
    the probe result that produced it."""
    sm = StateManager(tmp_path / "state.json")
    await sm.record_alert_outcome("https://never-seen.example", delivered=False)


# =============================================================================
# C. Surfacing it to whatever supervises the process
# =============================================================================


def test_undelivered_alerts_counts_only_pending(tmp_path: Path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"schema_version": main.STATE_SCHEMA_VERSION, "targets": {
        "https://a.com": {"status": "DOWN", "alert_pending": True},
        "https://b.com": {"status": "DOWN", "alert_pending": False},
        "https://c.com": {"status": "UP"},
        "https://d.com": "not a dict",
    }}), encoding="utf-8")
    assert undelivered_alerts(f) == 1


def test_undelivered_alerts_on_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """A fresh deployment has no state file. "Nothing known to be pending" is
    the honest answer, and it must not crash the exit-code path."""
    assert undelivered_alerts(tmp_path / "nope.json") == 0


def test_exit_code_reports_alerts_that_reached_nobody() -> None:
    """Targets being DOWN is normal operation and stays behind --strict. An
    alert reaching nobody is a failure OF the monitor, invisible by
    construction, so the supervisor must see it either way."""
    assert _exit_code(strict=False, down_count=5, undelivered=0) == 0
    assert _exit_code(strict=False, down_count=0, undelivered=1) == 1
    assert _exit_code(strict=True,  down_count=0, undelivered=0) == 0
    assert _exit_code(strict=True,  down_count=3, undelivered=0) == 1


# =============================================================================
# D. Circuit breaker
# =============================================================================


async def test_circuit_opens_after_repeated_failure_in_one_run() -> None:
    """With a channel down, every target otherwise pays the full retry policy
    independently. Six targets absorb that inside the run budget; thirty do
    not, and then the probe skips cycles because the ALERT path is slow."""
    reset_circuits()
    dead = _Channel("Dead", False)
    for _ in range(CIRCUIT_BREAK_AFTER):
        await dispatch_alert(None, URL, "d", AlertKind.FAILURE, notifiers=[dead])
    assert dead.calls == CIRCUIT_BREAK_AFTER

    await dispatch_alert(None, URL, "d", AlertKind.FAILURE, notifiers=[dead])
    assert dead.calls == CIRCUIT_BREAK_AFTER, "circuit open: no further attempts"


async def test_an_open_circuit_still_counts_the_alert_as_lost() -> None:
    """Failing fast skips the re-proving, never the accounting. A skipped
    alert is still an alert nobody received, and must still arm the retry."""
    reset_circuits()
    dead = _Channel("Dead", False)
    for _ in range(CIRCUIT_BREAK_AFTER + 1):
        result = await dispatch_alert(
            None, URL, "d", AlertKind.FAILURE, notifiers=[dead])
    assert result is False


async def test_a_dead_channel_does_not_break_a_healthy_sibling() -> None:
    """Breaking is per-channel on purpose: the entire reason for two channels
    is that one failing must not affect the other."""
    reset_circuits()
    dead, live = _Channel("Dead", False), _Channel("Live", True)
    for _ in range(CIRCUIT_BREAK_AFTER + 2):
        result = await dispatch_alert(
            None, URL, "d", AlertKind.FAILURE, notifiers=[dead, live])
    assert result is True
    assert live.calls == CIRCUIT_BREAK_AFTER + 2, "healthy channel never skipped"
    assert dead.calls == CIRCUIT_BREAK_AFTER


async def test_each_run_starts_with_the_circuit_closed() -> None:
    """A single bad minute must not mute a channel for good. The breaker is
    scoped to one run, and main.py resets it at the top of every one."""
    reset_circuits()
    dead = _Channel("Dead", False)
    for _ in range(CIRCUIT_BREAK_AFTER + 1):
        await dispatch_alert(None, URL, "d", AlertKind.FAILURE, notifiers=[dead])
    before = dead.calls

    reset_circuits()
    await dispatch_alert(None, URL, "d", AlertKind.FAILURE, notifiers=[dead])
    assert dead.calls == before + 1, "a new run must try again"
