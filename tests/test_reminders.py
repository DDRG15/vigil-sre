"""
tests/test_reminders.py — re-alerting while an outage is still going.

Why this exists
---------------
Alerting once on the transition is right against noise and wrong against a
six-hour outage: after the first message, silence and "resolved" look
identical to whoever is reading. So a target can opt in to being reminded on a
widening schedule — 1h, 2h, 4h, 6h, 12h, then daily.

Opt-in, not opt-out, and that is the load-bearing part. A target added to watch
something break — a test URL, a known-bad endpoint — would otherwise nag
forever, which is the alert fatigue this project has spent several rounds
removing.
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
from diagnostics import REMINDER_REPEAT_H, REMINDER_SCHEDULE_H, reminders_due
from main import StateManager

URL = "https://example.com"


async def _down_for(tmp_path: Path, hours: float, step: int = 0) -> StateManager:
    """A target confirmed DOWN *hours* ago, with *step* reminders already sent."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(URL, "boom")
    started = datetime.now(timezone.utc) - timedelta(hours=hours)
    sm._state[URL]["last_changed"] = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    sm._state[URL]["remind_step"]  = step
    return sm


# =============================================================================
# A. The schedule, as a pure function
# =============================================================================


@pytest.mark.parametrize("hours,expected", [
    (0,     0),
    (0.99,  0),   # just under the first threshold: silence
    (1,     1),
    (1.99,  1),
    (2,     2),
    (4,     3),
    (6,     4),
    (11.99, 4),   # just under the last fixed step
    (12,    5),
    (35,    5),   # 12h + 23h: the daily step has not come round yet
    (36,    6),   # 12h + 24h
    (60,    7),
    (84,    8),
])
def test_the_schedule_widens_then_settles_at_daily(hours, expected) -> None:
    """Tested on both sides of each threshold. A reminder that fires is half a
    test; one that stays quiet just under its trigger is the other half.

    The gaps widen on purpose: the spacing itself says "this has been going a
    while", and a fixed hourly ping is nagging by hour six."""
    assert reminders_due(hours) == expected


def test_the_schedule_is_a_count_not_a_window() -> None:
    """The property that makes it self-correcting. A monitor that was itself
    down for six hours comes back and sees three reminders were owed -- it
    sends ONE, not three and not zero. Asking "is one due right now" would skip
    a reminder forever whenever no run landed inside its window."""
    assert reminders_due(6) == 4
    assert reminders_due(6) > reminders_due(1), "elapsed time only adds"
    assert all(
        reminders_due(h) <= reminders_due(h + 1) for h in range(0, 100)
    ), "the count never goes backwards"


def test_negative_elapsed_owes_nothing() -> None:
    """Clock skew, or a last_changed in the future after a hand edit. Neither
    is a reason to alert."""
    assert reminders_due(-5) == 0


def test_the_schedule_matches_what_the_constants_say() -> None:
    """Guards against the table and the code drifting apart."""
    for threshold in REMINDER_SCHEDULE_H:
        assert reminders_due(threshold) >= 1
    assert reminders_due(REMINDER_SCHEDULE_H[-1] + REMINDER_REPEAT_H) == (
        len(REMINDER_SCHEDULE_H) + 1
    )


# =============================================================================
# B. Claiming a reminder
# =============================================================================


async def test_a_reminder_is_owed_once_the_outage_is_old_enough(tmp_path) -> None:
    sm = await _down_for(tmp_path, hours=4)
    elapsed = await sm.claim_reminder(URL)
    assert elapsed is not None and 3.9 < elapsed < 4.1


async def test_a_fresh_outage_owes_nothing(tmp_path) -> None:
    """The transition alert just fired. Reminding thirty seconds later would be
    the duplicate this project removes everywhere else."""
    sm = await _down_for(tmp_path, hours=0.2)
    assert await sm.claim_reminder(URL) is None


async def test_claiming_advances_the_counter(tmp_path) -> None:
    """Claims rather than asks: the counter moves inside the same lock, so two
    concurrent probes of one target cannot both decide to send it."""
    sm = await _down_for(tmp_path, hours=4)
    assert await sm.claim_reminder(URL) is not None
    assert await sm.claim_reminder(URL) is None, "already claimed this step"
    assert sm._state[URL]["remind_step"] == reminders_due(4)


async def test_the_next_step_becomes_claimable(tmp_path) -> None:
    sm = await _down_for(tmp_path, hours=13, step=reminders_due(12))
    assert await sm.claim_reminder(URL) is None, "still inside the 12h step"

    sm._state[URL]["last_changed"] = (
        datetime.now(timezone.utc) - timedelta(hours=37)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert await sm.claim_reminder(URL) is not None, "the daily step came round"


async def test_a_target_that_is_not_down_is_never_reminded(tmp_path) -> None:
    """DEGRADED is excluded deliberately: this project treats it as a
    performance signal, not an availability one -- uptime counts DEGRADED as
    up. A slow endpoint is not an incident to page about again."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_degraded(URL, "slow")
    sm._state[URL]["last_changed"] = (
        datetime.now(timezone.utc) - timedelta(hours=48)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert await sm.claim_reminder(URL) is None

    await sm.set_up(URL)
    assert await sm.claim_reminder(URL) is None


async def test_an_unknown_target_is_not_an_error(tmp_path) -> None:
    sm = StateManager(tmp_path / "state.json")
    assert await sm.claim_reminder("https://never-seen.example") is None


async def test_a_malformed_timestamp_owes_nothing(tmp_path) -> None:
    """A hand-edited state.json. Failing to parse must mean "no reminder", not
    a crash on the probe's critical path."""
    sm = await _down_for(tmp_path, hours=4)
    sm._state[URL]["last_changed"] = "not a timestamp"
    assert await sm.claim_reminder(URL) is None


# =============================================================================
# C. Reset across the outage boundary
# =============================================================================


async def test_the_counter_survives_a_repeat_failure(tmp_path) -> None:
    """set_down REBUILDS the record on every failed probe. Losing the counter
    there would re-send the 1h reminder every single run."""
    sm = await _down_for(tmp_path, hours=4)
    await sm.claim_reminder(URL)
    claimed = sm._state[URL]["remind_step"]

    await sm.set_down(URL, "boom again")
    assert sm._state[URL]["remind_step"] == claimed


async def test_recovery_clears_the_counter(tmp_path) -> None:
    """The counter belongs to ONE outage. Carrying it into UP would silence the
    first reminder of the next one -- which is why it is handled beside
    consecutive_failures and not in _CARRY_FORWARD_FIELDS."""
    sm = await _down_for(tmp_path, hours=13)
    await sm.claim_reminder(URL)
    assert sm._state[URL]["remind_step"] > 0

    await sm.set_up(URL)
    assert sm._state[URL].get("remind_step", 0) == 0

    # Hysteresis: coming from UP, DOWN_CONFIRMATIONS consecutive failures are
    # required before the target is confirmed down again. Driving it with one
    # failure would be testing a state the system never reaches.
    for _ in range(main.DOWN_CONFIRMATIONS):
        await sm.set_down(URL, "down again")
    assert sm._state[URL]["status"] == "DOWN"
    sm._state[URL]["last_changed"] = (
        datetime.now(timezone.utc) - timedelta(hours=1.5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert await sm.claim_reminder(URL) is not None, "a new outage starts fresh"


async def test_the_counter_is_persisted_not_just_in_memory(tmp_path) -> None:
    sm = await _down_for(tmp_path, hours=4)
    await sm.claim_reminder(URL)
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert stored["targets"][URL]["remind_step"] == reminders_due(4)
    assert stored["schema_version"] == main.STATE_SCHEMA_VERSION
