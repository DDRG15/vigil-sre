"""
tests/test_heartbeat_undelivered.py -- a round in which every alert was lost
must not tell the dead-man's switch that it is alive and well.

The heartbeat was withheld only for RunReport.incomplete (a probe crashed, a
state write failed). A round where every channel rejected the alerts -- a
rotated webhook answering 401 is the usual cause -- still pinged success and
then exited 1. The two consumers of "this round could not deliver" disagreed:
the supervisor saw a failing monitor and healthchecks.io saw a perfect one, so
the watchdog that exists to catch exactly this kind of silent failure stayed
green for as long as the webhook stayed dead.

Both now ask the same question (main._alerts_undelivered): status alerts still
owed in state.json plus the advisory alerts this round lost.

Decisions pinned here:
  - A target that is DOWN and whose alert WAS delivered is a complete round:
    the heartbeat watches the monitor, not its targets.
  - A muted alert is not a failure.
  - A status alert left over from an earlier round that is STILL undelivered
    withholds the heartbeat, exactly as it keeps the exit code at 1.

Coverage:
  A. The round that lost its alerts
  B. The rounds that did not
  C. A leftover from an earlier round
  D. Advisory alerts (certificate)
  E. Both consumers agree
  F. The line the on-call reads
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import ProbePhases
from main import Target
from notifiers import AlertKind

URL   = "https://svc.example"
NIGHT = {"start": "02:00", "end": "04:00"}


class _World:
    def __init__(self) -> None:
        self.probe     = "fail"       # "fail" | "ok" | "cert"
        self.delivers  = True
        self.window    = False
        self.sent: list[str] = []

    async def dispatch(self, session, url, status_detail, kind) -> bool:
        self.sent.append(kind.name)
        return self.delivers

    async def probe_impl(self, *a, **k) -> ProbePhases:
        if self.probe == "fail":
            raise main._ProbeFailure("HTTP 503 (expected 200)")
        return ProbePhases(
            url=URL, http_status=200, ttfb_ms=50.0, transfer_ms=1.0,
            body_bytes=10, tls_cert_days_left=20 if self.probe == "cert" else None,
        )

    def in_maintenance(self, windows, now):
        return NIGHT if self.window else None


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
def heartbeat(monkeypatch) -> AsyncMock:
    hb = AsyncMock()
    monkeypatch.setattr(main, "_send_heartbeat", hb)
    return hb


async def _round(tmp_path: Path) -> tuple[int, main.RunReport, Path]:
    state_path = tmp_path / "state.json"
    report     = main.RunReport()
    down = await main.run_health_checks(
        targets=[Target(url=URL, maintenance=[NIGHT])],
        state_path=state_path, history_path=tmp_path / "h.db", report=report)
    return down, report, state_path


# =============================================================================
# A. The round that lost its alerts
# =============================================================================


async def test_a_round_whose_alert_reached_nobody_withholds_the_heartbeat(
        tmp_path, world, heartbeat) -> None:
    """The defect: every channel rejected the FAILURE (a 401 after a webhook
    rotation) and the round still pinged success."""
    world.delivers = False
    down, report, state_path = await _round(tmp_path)

    assert down == 1
    assert main.undelivered_alerts(state_path) == 1, "the alert is owed"
    assert main._finish_run(False, down, state_path, report) == 1
    assert heartbeat.await_count == 0, "alive-and-well sent although nothing was delivered"


async def test_with_no_channel_configured_the_heartbeat_is_withheld_too(
        tmp_path, monkeypatch, heartbeat) -> None:
    """Not a special case: dispatch_alert reports 'nowhere to send' as False,
    which is exactly an alert that reached nobody."""
    async def _probe(*a, **k):
        raise main._ProbeFailure("HTTP 503 (expected 200)")

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "_probe_with_backoff", _probe)
    monkeypatch.setattr(main, "sample_connection", _no_sample)

    # No maintenance window: the real in_maintenance reads the wall clock, and
    # a window that happened to be open would mute the alert and hide the point.
    await main.run_health_checks(
        targets=[Target(url=URL)],
        state_path=tmp_path / "state.json", history_path=tmp_path / "h.db")

    assert heartbeat.await_count == 0


# =============================================================================
# B. The rounds that did not lose anything
# =============================================================================


async def test_a_down_target_whose_alert_was_delivered_still_sends_the_heartbeat(
        tmp_path, world, heartbeat) -> None:
    down, report, state_path = await _round(tmp_path)

    assert down == 1 and world.sent == ["FAILURE"]
    assert heartbeat.await_count == 1, (
        "the heartbeat watches the monitor, not its targets")
    assert main._finish_run(False, down, state_path, report) == 0


async def test_a_muted_alert_is_not_a_failure(tmp_path, world, heartbeat) -> None:
    world.window = True
    down, report, state_path = await _round(tmp_path)

    assert down == 1 and world.sent == []
    assert heartbeat.await_count == 1
    assert main._finish_run(False, down, state_path, report) == 0


async def test_a_quiet_healthy_round_sends_the_heartbeat(
        tmp_path, world, heartbeat) -> None:
    world.probe = "ok"
    await _round(tmp_path)
    await _round(tmp_path)

    assert heartbeat.await_count == 2


# =============================================================================
# C. A leftover from an earlier round
# =============================================================================


async def test_a_leftover_alert_that_is_still_undelivered_withholds_the_heartbeat(
        tmp_path, world, heartbeat) -> None:
    """Round 1 loses the FAILURE. Round 2 probes the same DOWN target, retries,
    and the channel is still dead. The record that something is owed is two
    rounds old, and it is the exit code's reason too -- the heartbeat agrees."""
    world.delivers = False
    await _round(tmp_path)
    heartbeat.reset_mock()
    world.sent.clear()

    down, report, state_path = await _round(tmp_path)

    assert world.sent == ["FAILURE"], "the retry was attempted"
    assert main._finish_run(False, down, state_path, report) == 1
    assert heartbeat.await_count == 0


async def test_a_leftover_alert_that_the_retry_delivers_restores_the_heartbeat(
        tmp_path, world, heartbeat) -> None:
    world.delivers = False
    await _round(tmp_path)
    heartbeat.reset_mock()

    world.delivers = True
    down, report, state_path = await _round(tmp_path)

    assert main.undelivered_alerts(state_path) == 0
    assert heartbeat.await_count == 1
    assert main._finish_run(False, down, state_path, report) == 0


# =============================================================================
# D. Advisory alerts
# =============================================================================


async def test_a_lost_certificate_alert_withholds_the_heartbeat(
        tmp_path, world, heartbeat) -> None:
    """Advisory kinds do not write alert_pending; they are tallied for the
    round. They count toward the exit code, so they count here."""
    world.probe = "ok"
    await _round(tmp_path)                       # first sighting, healthy
    world.probe = "cert"
    heartbeat.reset_mock()

    world.delivers = False
    down, report, state_path = await _round(tmp_path)

    assert main.undelivered_alerts(state_path) == 0, "no status alert is owed"
    assert sum(report.undelivered_by_kind.values()) == 1
    assert main._finish_run(False, down, state_path, report) == 1
    assert heartbeat.await_count == 0


# =============================================================================
# E. Both consumers agree
# =============================================================================


@pytest.mark.parametrize("scenario", [
    "delivered", "all_channels_failed", "muted", "leftover_still_undelivered",
    "leftover_delivered", "healthy", "advisory_lost",
])
async def test_the_heartbeat_and_the_exit_code_never_disagree(
        tmp_path, world, heartbeat, scenario) -> None:
    """The defect was a disagreement, so the property is the test: across every
    way a round can end, 'heartbeat sent' is exactly 'exit code is 0'."""
    if scenario == "delivered":
        pass
    elif scenario == "all_channels_failed":
        world.delivers = False
    elif scenario == "muted":
        world.window = True
    elif scenario == "leftover_still_undelivered":
        world.delivers = False
        await _round(tmp_path)
    elif scenario == "leftover_delivered":
        world.delivers = False
        await _round(tmp_path)
        world.delivers = True
    elif scenario == "healthy":
        world.probe = "ok"
    elif scenario == "advisory_lost":
        world.probe = "ok"
        await _round(tmp_path)
        world.probe, world.delivers = "cert", False
    heartbeat.reset_mock()

    down, report, state_path = await _round(tmp_path)

    sent = heartbeat.await_count == 1
    assert sent == (main._finish_run(False, down, state_path, report) == 0), (
        f"{scenario}: heartbeat sent={sent}, exit code disagrees")


# =============================================================================
# F. The line the on-call reads
# =============================================================================


async def test_withholding_logs_one_error_with_a_metric_and_the_reason(
        tmp_path, world, heartbeat, caplog) -> None:
    world.delivers = False
    with caplog.at_level("ERROR", logger="sre.health_checker"):
        await _round(tmp_path)

    lines = [r for r in caplog.records if "heartbeats_withheld_total" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    assert lines[0].levelname == "ERROR"
    message = lines[0].getMessage()
    assert "reached NOBODY" in message and "heartbeat" in message.lower()
    assert "event_type=metric metric=heartbeats_withheld_total value=1" in message


async def test_a_delivered_round_logs_no_withheld_line(
        tmp_path, world, heartbeat, caplog) -> None:
    with caplog.at_level("ERROR", logger="sre.health_checker"):
        await _round(tmp_path)
    assert not [r for r in caplog.records
                if "heartbeats_withheld_total" in r.getMessage()]
