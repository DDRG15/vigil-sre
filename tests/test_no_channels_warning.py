"""
tests/test_no_channels_warning.py -- a monitor with nowhere to send an alert says so
every round, not only once something is already down.

With no DISCORD_WEBHOOK_URL and no SLACK_WEBHOOK_URL the monitor probed, logged
UP, wrote history and looked healthy in every way. The first sign that nobody
would ever be told anything was a CRITICAL "alert ... is LOST" -- written at the
moment an outage had already happened. Found on Kubernetes, where a pod without
its Secret ran green until the first DOWN put it into CrashLoopBackOff.

The heartbeat already warns every round when HEARTBEAT_URL is unset, for the
same reason: an unset variable and a forgotten one look identical from inside.
"""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from main import Target

WARNING_TEXT = "No alert channels configured"


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(main, "RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(main, "_send_heartbeat", AsyncMock())

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)

    async def _up(*a, **k):
        return main.CheckOutcome(url=a[0], status=main.STATUS_UP,
                                 checked_at="2026-11-01T00:00:00Z", phases=None)
    monkeypatch.setattr(main, "check_url", _up)


async def _round(tmp_path):
    await main.run_health_checks(
        targets=[Target(url="https://example.test/")],
        state_path=tmp_path / "state.json", history_path=tmp_path / "h.db")


async def test_no_channel_is_a_warning_on_a_healthy_round(tmp_path, monkeypatch, caplog, quiet):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        await _round(tmp_path)
    hits = [r for r in caplog.records if WARNING_TEXT in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    assert hits[0].levelno == logging.WARNING
    message = hits[0].getMessage()
    assert "DISCORD_WEBHOOK_URL" in message and "SLACK_WEBHOOK_URL" in message
    assert "metric=alert_channels_unconfigured_total" in message


@pytest.mark.parametrize("variable", ["DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL"])
async def test_one_channel_is_enough_to_stay_quiet(tmp_path, monkeypatch, caplog, quiet, variable):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setenv(variable, "http://127.0.0.1:9/hook")
    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        await _round(tmp_path)
    assert not [r for r in caplog.records if WARNING_TEXT in r.getMessage()]
