"""
tests/test_delivery_edges.py -- a delivered alert reported as lost.

A Discord webhook URL ending ``?wait=true`` answers 200 with the message body
instead of 204. The old check was ``status == 204``, so a delivered alert was
reported lost, kept ``alert_pending``, and was re-sent every round. Any 2xx is a
delivery, on every channel; the retryable / non-retryable split (429 and 5xx
retry, other 4xx abandon) is unchanged and pinned here too.

No test opens a socket: aioresponses answers every request and asyncio.sleep is
replaced.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notifiers import AlertKind, DiscordNotifier, SlackNotifier, reset_circuits

TARGET_URL        = "https://example.com"
DISCORD_URL       = "https://discord.com/api/webhooks/test/token"
DISCORD_WAIT_URL  = DISCORD_URL + "?wait=true"
SLACK_URL         = "https://hooks.slack.com/services/TEST/TEST/token"


@pytest.fixture(autouse=True)
def _fresh_circuits():
    """The breaker is module state; a test that trips it must not leak."""
    reset_circuits()
    yield
    reset_circuits()


async def test_discord_200_with_wait_true_is_a_delivery(monkeypatch) -> None:
    """``?wait=true`` makes Discord answer 200 with the created message. The
    alert is in the channel; reporting it lost re-sends it every round."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_WAIT_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(DISCORD_WAIT_URL, status=200, body='{"id": "123", "content": ""}')
            async with aiohttp.ClientSession() as session:
                delivered = await DiscordNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
        mock_sleep.assert_not_called()
    assert delivered is True


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
@pytest.mark.parametrize("notifier_cls, env_var, url", [
    (DiscordNotifier, "DISCORD_WEBHOOK_URL", DISCORD_URL),
    (SlackNotifier,   "SLACK_WEBHOOK_URL",   SLACK_URL),
])
async def test_every_2xx_is_success_on_every_channel(
    monkeypatch, notifier_cls, env_var: str, url: str, status: int
) -> None:
    """One POST, no sleep, True -- for each channel and each 2xx. A channel that
    assumed its sibling's success code is the bug this replaces."""
    monkeypatch.setenv(env_var, url)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(url, status=status)
            async with aiohttp.ClientSession() as session:
                delivered = await notifier_cls().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
            assert sum(len(v) for v in mock.requests.values()) == 1
        mock_sleep.assert_not_called()
    assert delivered is True


@pytest.mark.parametrize("status", [199, 300, 301, 400, 401, 403, 404])
async def test_non_2xx_below_500_is_not_a_delivery_and_not_retried(
    monkeypatch, status: int
) -> None:
    """Classification is unchanged: 4xx (and anything else that is neither 2xx,
    429 nor 5xx) is abandoned after one POST."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(DISCORD_URL, status=status)
            async with aiohttp.ClientSession() as session:
                delivered = await DiscordNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
            assert sum(len(v) for v in mock.requests.values()) == 1
        mock_sleep.assert_not_called()
    assert delivered is False


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_retryable_statuses_still_retry_then_succeed(
    monkeypatch, status: int
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(DISCORD_URL, status=status)
            mock.post(DISCORD_URL, status=200, body="{}")
            async with aiohttp.ClientSession() as session:
                delivered = await DiscordNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
        mock_sleep.assert_called_once_with(1.0)
    assert delivered is True
