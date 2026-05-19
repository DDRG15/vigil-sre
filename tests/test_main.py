"""
tests/test_main.py — Test suite for vigil-sre health checker.

Coverage:
  A. load_targets()            — 5 tests  (sync)
  B. StateManager              — 7 tests  (async)
  C. _probe_once()             — 5 tests  (async, aioresponses mock)
  D. _probe_with_backoff()     — 3 tests  (async, sleep mocked)
  E. _build_discord_payload()  — 3 tests  (sync)

Run: pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    StateManager,
    _ProbeFailure,
    _build_discord_payload,
    _probe_once,
    _probe_with_backoff,
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
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_up(TARGET_URL)
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert TARGET_URL in data
    assert data[TARGET_URL]["status"] == "UP"


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
# =============================================================================


async def test_backoff_success_first_try() -> None:
    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=200)
            async with aiohttp.ClientSession() as session:
                await _probe_with_backoff(session, TARGET_URL)  # must not raise


async def test_backoff_fail_then_success() -> None:
    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=200)
            async with aiohttp.ClientSession() as session:
                await _probe_with_backoff(session, TARGET_URL)  # must not raise


async def test_backoff_all_fail() -> None:
    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with pytest.raises(_ProbeFailure):
                    await _probe_with_backoff(session, TARGET_URL)


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
