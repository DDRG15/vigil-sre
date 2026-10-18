"""
tests/test_first_sight.py — a target the monitor has never seen was never down.

Why this exists
---------------
A recovery means "it was DOWN and now it is not". On a fresh install, or the
first run after a target is added, there is no previous record, and set_up read
"no previous record" as a status change: every healthy target paged a
"Service Recovered" for an outage that never happened. Reproduced on
a clean clone, one run, three targets, three false recoveries.

The fix is at the decision point (StateManager.set_up), not in the notifier:
the notifier is told what kind of alert to send, and should never have to
second-guess whether the transition behind it was real.

What stays the same, because both are real problem states rather than recoveries:
  - unknown -> DOWN alerts, straight away (no DOWN_CONFIRMATIONS wait: there is
    no flapping to protect against on a first observation, and the README's
    smoke-test targets rely on it);
  - unknown -> DEGRADED is recorded immediately and alerts after confirmation.
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

URL = "https://svc.example"


class _Sent:
    """Stands in for dispatch_alert and records the KIND of every alert."""

    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.delivers = True

    async def __call__(self, session, url, status_detail, kind) -> bool:
        self.kinds.append(kind.name)
        return self.delivers


def _phases(ttfb: float = 50.0) -> ProbePhases:
    return ProbePhases(
        url=URL, http_status=200, ttfb_ms=ttfb, transfer_ms=1.0,
        body_bytes=10, tls_cert_days_left=None,
    )


@pytest.fixture
def sent(monkeypatch) -> _Sent:
    spy = _Sent()
    monkeypatch.setattr(main, "dispatch_alert", spy)

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    return spy


def _probe_ok(monkeypatch, ttfb: float = 50.0) -> None:
    async def _probe(*a, **k):
        return _phases(ttfb)
    monkeypatch.setattr(main, "_probe_with_backoff", _probe)


def _probe_fail(monkeypatch) -> None:
    async def _probe(*a, **k):
        raise main._ProbeFailure("HTTP 503 (expected 200)")
    monkeypatch.setattr(main, "_probe_with_backoff", _probe)


async def _round(state: main.StateManager) -> main.CheckOutcome:
    return await main.check_url(URL, None, state)


# =============================================================================
# A. unknown -> UP is not a recovery
# =============================================================================


async def test_a_healthy_target_seen_for_the_first_time_sends_nothing(
    tmp_path, monkeypatch, sent
) -> None:
    """The defect, end to end through check_url: empty state, healthy probe."""
    _probe_ok(monkeypatch)
    state = main.StateManager(tmp_path / "state.json")

    outcome = await _round(state)

    assert outcome.status == main.STATUS_UP
    assert sent.kinds == [], f"a false recovery was sent: {sent.kinds}"


async def test_the_first_sight_is_still_recorded(tmp_path, monkeypatch, sent) -> None:
    """Silence is about the ALERT. The status, the timestamps and the file are
    written exactly as before -- the dashboard and uptime need the record."""
    _probe_ok(monkeypatch)
    path  = tmp_path / "state.json"
    state = main.StateManager(path)
    await _round(state)

    record = json.loads(path.read_text(encoding="utf-8"))["targets"][URL]
    assert record["status"] == main.STATUS_UP
    assert record["last_checked"] and record["last_changed"]
    assert record["last_error"] == ""


async def test_the_first_sight_does_not_arm_the_retry(tmp_path, monkeypatch, sent) -> None:
    """No alert went out, so none can be 'lost': alert_pending stays unset, or
    the next run would send the recovery this fix removed, one cycle late.
    Every channel is down here, which is the case that would arm the flag if
    an alert were attempted at all."""
    _probe_ok(monkeypatch)
    sent.delivers = False
    state = main.StateManager(tmp_path / "state.json")
    await _round(state)
    await _round(state)

    assert not state._state[URL].get("alert_pending")
    assert sent.kinds == []


async def test_set_up_reports_no_transition_for_an_unknown_target(tmp_path) -> None:
    """The decision point itself, without the orchestration around it."""
    state = main.StateManager(tmp_path / "state.json")
    assert await state.set_up(URL) is False


# =============================================================================
# B. What a real recovery still does
# =============================================================================


async def test_a_real_recovery_still_alerts(tmp_path, monkeypatch, sent) -> None:
    """The other half of the fix. A target that was DOWN and now is not has to
    be announced, or the fix traded a false page for a missing one."""
    state = main.StateManager(tmp_path / "state.json")
    await state.set_down(URL, "boom")
    await state.record_alert_outcome(URL, True)

    _probe_ok(monkeypatch)
    await _round(state)

    assert sent.kinds == ["RECOVERY"]


async def test_a_degraded_target_that_heals_still_alerts(tmp_path, monkeypatch, sent) -> None:
    state = main.StateManager(tmp_path / "state.json")
    await state.set_degraded(URL, "slow")
    await state.record_alert_outcome(URL, True)

    _probe_ok(monkeypatch)
    for _ in range(main.PERFORMANCE_RECOVERY_CONFIRMATIONS):
        await _round(state)

    assert sent.kinds == ["PERFORMANCE_RECOVERY"]


async def test_a_lost_recovery_is_still_retried(tmp_path) -> None:
    """alert_pending after a real recovery still re-sends. Only the 'no record'
    case lost its alert; the 'record says undelivered' case did not."""
    state = main.StateManager(tmp_path / "state.json")
    await state.set_down(URL, "boom")
    assert await state.set_up(URL) is True
    await state.record_alert_outcome(URL, False)
    assert await state.set_up(URL) is True


# =============================================================================
# C. The other two first sights keep alerting
# =============================================================================


async def test_a_first_failure_goes_straight_to_down_and_alerts(
    tmp_path, monkeypatch, sent
) -> None:
    """unknown -> DOWN: no DOWN_CONFIRMATIONS wait, one FAILURE. This is the
    README's smoke test (a fresh install's broken targets alert once)."""
    _probe_fail(monkeypatch)
    state = main.StateManager(tmp_path / "state.json")

    outcome = await _round(state)

    assert outcome.status == main.STATUS_DOWN
    assert sent.kinds == ["FAILURE"]
    assert state._state[URL]["consecutive_failures"] == 1


async def test_a_first_failure_alerts_once_not_every_run(tmp_path, monkeypatch, sent) -> None:
    _probe_fail(monkeypatch)
    state = main.StateManager(tmp_path / "state.json")
    await _round(state)
    await _round(state)
    await _round(state)

    assert sent.kinds == ["FAILURE"]


async def test_a_first_slow_probe_is_recorded_then_confirmed_before_alerting(tmp_path, monkeypatch, sent) -> None:
    """A slow observation is visible before it justifies a notification."""
    _probe_ok(monkeypatch, ttfb=5000.0)
    state = main.StateManager(tmp_path / "state.json")

    outcome = await _round(state)

    assert outcome.status == main.STATUS_DEGRADED
    assert sent.kinds == []
    for _ in range(main.DEGRADED_CONFIRMATIONS - 1):
        await _round(state)
    assert sent.kinds == ["DEGRADED"]
