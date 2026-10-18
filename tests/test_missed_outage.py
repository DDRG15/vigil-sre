"""
tests/test_missed_outage.py — an outage nobody was told about is announced once.

If the FAILURE for an outage reached no channel (every webhook down or
rejecting) and the target then recovered, nothing was ever said: the recovery
was not announced either, because there is no closure for an opening that was
never delivered. The lost FAILURE showed only as exit 1 and a withheld
heartbeat while it lasted, so afterwards nobody could know an outage happened.

On leaving DOWN with the FAILURE undelivered, one catch-up notice is owed: the
outage's start and end, and that its alert never arrived. It is a status alert
like the others — retried until a channel accepts it, cleared only on delivery,
dropped by a maintenance mute — and an outage whose FAILURE WAS delivered still
recovers with an ordinary RECOVERY.
"""

from __future__ import annotations

import pytest

import main
import notifiers
from main import STATUS_DEGRADED, AlertKind, StateManager

URL = "https://a.example"


async def _confirmed_down(state: StateManager, delivered: bool) -> None:
    """A known-UP target confirmed DOWN, with its FAILURE delivered or not."""
    await state.set_up(URL, stabilize=True)          # first sighting: silent
    for _ in range(main.DOWN_CONFIRMATIONS):
        alert = await state.set_down(URL, error="Connection refused", stabilize=True)
    assert alert is True, "the FAILURE was never decided"
    await state.record_alert_outcome(URL, delivered=delivered)


@pytest.mark.asyncio
async def test_recovering_from_an_unannounced_outage_owes_one_catch_up(tmp_path):
    state = StateManager(tmp_path / "state.json")
    await _confirmed_down(state, delivered=False)

    assert await state.set_up(URL, stabilize=True) is True, "the recovery stayed silent"
    kind, detail = state.status_alert(URL, AlertKind.RECOVERY, "Service is UP")
    assert kind is AlertKind.UNANNOUNCED_OUTAGE
    missed = state.missed_outage(URL)
    assert missed and missed["start"] and missed["end"]
    assert missed["start"] in detail and missed["end"] in detail
    assert "never reached" in detail


@pytest.mark.asyncio
async def test_a_delivered_outage_recovers_with_an_ordinary_recovery(tmp_path):
    state = StateManager(tmp_path / "state.json")
    await _confirmed_down(state, delivered=True)

    assert await state.set_up(URL, stabilize=True) is True
    kind, _ = state.status_alert(URL, AlertKind.RECOVERY, "Service is UP")
    assert kind is AlertKind.RECOVERY
    assert state.missed_outage(URL) is None


@pytest.mark.asyncio
async def test_a_lost_catch_up_is_retried_until_delivered_then_cleared(tmp_path):
    state = StateManager(tmp_path / "state.json")
    await _confirmed_down(state, delivered=False)
    assert await state.set_up(URL, stabilize=True) is True
    first = state.missed_outage(URL)
    await state.record_alert_outcome(URL, delivered=False)      # catch-up lost too

    # Next round, still UP: the same notice is owed again, unchanged.
    reloaded = StateManager(tmp_path / "state.json")
    assert await reloaded.set_up(URL, stabilize=True) is True
    assert reloaded.status_alert(URL, AlertKind.RECOVERY, "Service is UP")[0] is AlertKind.UNANNOUNCED_OUTAGE
    assert reloaded.missed_outage(URL) == first

    await reloaded.record_alert_outcome(URL, delivered=True)
    assert reloaded.missed_outage(URL) is None
    assert await reloaded.set_up(URL, stabilize=True) is False, "a delivered notice re-sent"


@pytest.mark.asyncio
async def test_leaving_an_unannounced_outage_into_degraded_also_owes_it(tmp_path):
    state = StateManager(tmp_path / "state.json")
    await _confirmed_down(state, delivered=False)

    assert await state.set_degraded(URL, "SLOW_BACKEND", stabilize=True) is True
    kind, detail = state.status_alert(URL, AlertKind.DEGRADED, "SLOW_BACKEND")
    assert kind is AlertKind.UNANNOUNCED_OUTAGE
    assert STATUS_DEGRADED in detail


@pytest.mark.asyncio
async def test_a_maintenance_mute_does_not_erase_a_really_lost_outage(tmp_path):
    """The FAILURE was lost to dead channels, not muted. If the target recovers
    inside a window the catch-up is muted with it, but it stays owed and goes
    out on the first round after the window -- erasing it would let the outage
    pass unseen a second time."""
    state = StateManager(tmp_path / "state.json")
    await _confirmed_down(state, delivered=False)
    assert await state.set_up(URL, stabilize=True) is True

    await state.release_muted_status_alert(URL)
    assert state.missed_outage(URL) is not None
    assert await state.set_up(URL, stabilize=True) is True, "the muted notice was forgotten"
    assert state.status_alert(URL, AlertKind.RECOVERY, "")[0] is AlertKind.UNANNOUNCED_OUTAGE


@pytest.mark.parametrize("notifier", [notifiers.DiscordNotifier, notifiers.SlackNotifier])
def test_both_channels_render_the_catch_up(notifier):
    payload = notifier().build_payload(URL, "DOWN from a to b", "2026-10-17T00:00:00Z",
                                       AlertKind.UNANNOUNCED_OUTAGE)
    assert "Outage" in str(payload)
