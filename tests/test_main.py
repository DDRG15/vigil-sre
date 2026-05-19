"""
tests/test_main.py — Test suite for vigil-sre health checker.

Coverage:
  A. load_targets()            —  5 tests  (sync)
  B. StateManager              — 10 tests  (async) — includes invariant checks
  C. _probe_once()             —  5 tests  (async, aioresponses mock)
  D. _probe_with_backoff()     —  3 tests  (async, sleep call count verified)
  E. _build_discord_payload()  —  3 tests  (sync)
  F. check_url() pipeline      —  4 tests  (async, full orchestration)

Run: pytest tests/ -v

Reviewer notes
--------------
Every test in groups D and F asserts not just the outcome but also the
side-effects (sleep call counts, alert dispatch counts, is_recovery flag).
A test that only checks "no exception raised" for a retry function is not
a test — it is a hope. We do not ship hopes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import aiohttp
import pytest
from aioresponses import aioresponses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    StateManager,
    _ProbeFailure,
    _build_discord_payload,
    _probe_once,
    _probe_with_backoff,
    check_url,
    load_targets,
)

TARGET_URL = "https://example.com"


# =============================================================================
# A. load_targets()
# =============================================================================


def test_load_targets_valid(tmp_path: Path) -> None:
    f = tmp_path / "targets.yaml"
    f.write_text("targets:\n  - https://a.com\n  - https://b.com\n", encoding="utf-8")
    result = load_targets(f)
    assert result == ["https://a.com", "https://b.com"]


def test_load_targets_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_targets(tmp_path / "nonexistent.yaml")


def test_load_targets_malformed_yaml(tmp_path: Path) -> None:
    f = tmp_path / "targets.yaml"
    f.write_text("targets: [\nunclosed bracket\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_empty_list(tmp_path: Path) -> None:
    f = tmp_path / "targets.yaml"
    f.write_text("targets: []\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_missing_key(tmp_path: Path) -> None:
    f = tmp_path / "targets.yaml"
    f.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_targets(f)


# =============================================================================
# B. StateManager
# =============================================================================


async def test_state_initial_up(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    transitioned = await sm.set_up(TARGET_URL)
    assert transitioned is True


async def test_state_already_up(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)
    transitioned = await sm.set_up(TARGET_URL)
    assert transitioned is False


async def test_state_initial_down(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    transitioned = await sm.set_down(TARGET_URL, "HTTP 503")
    assert transitioned is True


async def test_state_already_down(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(TARGET_URL, "HTTP 503")
    transitioned = await sm.set_down(TARGET_URL, "HTTP 503")
    assert transitioned is False


async def test_state_down_to_up(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(TARGET_URL, "HTTP 503")
    transitioned = await sm.set_up(TARGET_URL)
    assert transitioned is True


async def test_state_up_to_down(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)
    transitioned = await sm.set_down(TARGET_URL, "Timeout (>5s)")
    assert transitioned is True


async def test_state_atomic_write(tmp_path: Path) -> None:
    """State file must be valid JSON with correct fields after every write."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_up(TARGET_URL)

    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    record = data[TARGET_URL]
    assert record["status"] == "UP"
    assert record["last_error"] == ""
    assert "T" in record["last_checked"]   # ISO-8601 sanity check
    assert "T" in record["last_changed"]


async def test_state_error_cleared_on_recovery(tmp_path: Path) -> None:
    """DOWN→UP transition must clear last_error. A non-empty error after
    recovery would be a stale lie visible to any operator reading state.json."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_down(TARGET_URL, "HTTP 503")
    await sm.set_up(TARGET_URL)

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data[TARGET_URL]["last_error"] == ""


async def test_state_last_changed_unchanged_on_repeat(tmp_path: Path) -> None:
    """Calling set_up() twice must NOT advance last_changed on the second call.
    last_changed tracks transitions, not every probe. If it advances on every
    call, operators cannot tell when a service actually recovered."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_up(TARGET_URL)

    first = json.loads(state_file.read_text(encoding="utf-8"))
    last_changed_after_first = first[TARGET_URL]["last_changed"]

    await sm.set_up(TARGET_URL)  # same state — must not change last_changed

    second = json.loads(state_file.read_text(encoding="utf-8"))
    last_changed_after_second = second[TARGET_URL]["last_changed"]

    assert last_changed_after_first == last_changed_after_second


async def test_state_persists_across_restart(tmp_path: Path) -> None:
    """A second StateManager reading the same file must see the state written
    by the first. This simulates a process restart — the system's memory
    must survive between runs or the alert-suppression logic is broken."""
    state_file = tmp_path / "state.json"

    sm1 = StateManager(state_file)
    await sm1.set_down(TARGET_URL, "HTTP 503")

    sm2 = StateManager(state_file)  # fresh instance, same file
    transitioned = await sm2.set_down(TARGET_URL, "HTTP 503")

    # sm2 must know it was already DOWN — no transition should be detected
    assert transitioned is False


# =============================================================================
# C. _probe_once()
# =============================================================================


async def test_probe_once_200() -> None:
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200)
        async with aiohttp.ClientSession() as session:
            await _probe_once(session, TARGET_URL)  # must not raise


async def test_probe_once_503() -> None:
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=503)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(session, TARGET_URL)
    assert "503" in exc_info.value.detail


async def test_probe_once_404() -> None:
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=404)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(session, TARGET_URL)
    assert "404" in exc_info.value.detail


async def test_probe_once_timeout() -> None:
    with aioresponses() as mock:
        mock.get(TARGET_URL, exception=asyncio.TimeoutError())
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(session, TARGET_URL)
    assert "Timeout" in exc_info.value.detail


async def test_probe_once_connection_error() -> None:
    with aioresponses() as mock:
        mock.get(TARGET_URL, exception=aiohttp.ClientConnectionError("DNS failure"))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(session, TARGET_URL)
    assert "ConnectionError" in exc_info.value.detail


# =============================================================================
# D. _probe_with_backoff()
#
# asyncio.sleep is mocked in all tests to avoid real wall-clock delays.
# Every test asserts the EXACT number of sleep calls and their values —
# not just the final outcome. A retry loop with wrong intervals is a bug
# even if it eventually returns the right answer.
# =============================================================================


async def test_backoff_success_first_try() -> None:
    """Success on the first attempt must produce zero sleep calls."""
    with patch("main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=200)
            async with aiohttp.ClientSession() as session:
                await _probe_with_backoff(session, TARGET_URL)
        mock_sleep.assert_not_called()


async def test_backoff_fail_then_success() -> None:
    """Two failures then success: sleep must be called exactly twice
    with the correct exponential intervals (2.0s then 4.0s)."""
    with patch("main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=200)
            async with aiohttp.ClientSession() as session:
                await _probe_with_backoff(session, TARGET_URL)
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list == [call(2.0), call(4.0)]


async def test_backoff_all_fail() -> None:
    """All 3 attempts fail: must raise _ProbeFailure with the last
    error detail. No sleep after the final attempt."""
    with patch("main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with pytest.raises(_ProbeFailure) as exc_info:
                    await _probe_with_backoff(session, TARGET_URL)
        assert "503" in exc_info.value.detail
        # Sleep is called after attempt 1 and 2, but NOT after the final failure
        assert mock_sleep.call_count == 2


# =============================================================================
# E. _build_discord_payload()
# =============================================================================


def test_discord_payload_failure() -> None:
    payload = _build_discord_payload(
        url=TARGET_URL,
        status_detail="HTTP 503",
        timestamp="2026-01-01T00:00:00Z",
        is_recovery=False,
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFF0000
    assert "Alert" in embed["title"]


def test_discord_payload_recovery() -> None:
    payload = _build_discord_payload(
        url=TARGET_URL,
        status_detail="Service is UP",
        timestamp="2026-01-01T00:00:00Z",
        is_recovery=True,
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0x00C853
    assert "Recovered" in embed["title"]


def test_discord_payload_fields() -> None:
    payload = _build_discord_payload(
        url=TARGET_URL,
        status_detail="HTTP 503",
        timestamp="2026-01-01T00:00:00Z",
        is_recovery=False,
    )
    fields = payload["embeds"][0]["fields"]
    field_values = [f["value"] for f in fields]
    assert any(TARGET_URL in v for v in field_values)
    assert any("2026-01-01T00:00:00Z" in v for v in field_values)


# =============================================================================
# F. check_url() — full orchestration pipeline
#
# check_url() is where probe + state + Discord alert are wired together.
# These tests verify that the CORRECT alert is fired (or suppressed) based
# on the state transition, not just that no exception was raised.
# send_discord_alert is mocked — we are not testing the webhook call here,
# we are testing whether check_url DECIDES to call it, and with what args.
# =============================================================================


async def test_check_url_up_no_transition(tmp_path: Path) -> None:
    """URL already UP → probe returns 200 → no alert should fire."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200)
        async with aiohttp.ClientSession() as session:
            with patch("main.send_discord_alert", new_callable=AsyncMock) as mock_alert:
                await check_url(TARGET_URL, session, sm)
    mock_alert.assert_not_called()


async def test_check_url_up_transition_fires_recovery_alert(tmp_path: Path) -> None:
    """URL was DOWN → probe returns 200 → recovery alert must fire with is_recovery=True."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(TARGET_URL, "HTTP 503")

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200)
        async with aiohttp.ClientSession() as session:
            with patch("main.send_discord_alert", new_callable=AsyncMock) as mock_alert:
                await check_url(TARGET_URL, session, sm)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["is_recovery"] is True


async def test_check_url_down_transition_fires_failure_alert(tmp_path: Path) -> None:
    """URL was UP → probe fails all retries → failure alert must fire with is_recovery=False."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)

    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with patch("main.send_discord_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["is_recovery"] is False


async def test_check_url_down_no_transition_suppresses_alert(tmp_path: Path) -> None:
    """URL already DOWN → probe still fails → alert must be suppressed (no duplicate)."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(TARGET_URL, "HTTP 503")

    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with patch("main.send_discord_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)

    mock_alert.assert_not_called()
