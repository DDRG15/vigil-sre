"""
tests/test_retry_after.py -- a Retry-After header that is not a usable number.

A ``Retry-After: nan`` header parsed to float('nan'). ``min(nan, 30.0)`` is nan
(every comparison with nan is False, so min() keeps its first argument), and
``asyncio.sleep(nan)`` is not a bounded sleep: the round never finishes, so no
heartbeat is sent and the dead-man's switch fires on a monitor that is alive and
stuck. Negative values slept zero and retried at once, the opposite of what the
server asked for.

No test opens a socket: aioresponses answers every request and asyncio.sleep is
replaced, so a sleep argument is observable instead of waited.
"""

from __future__ import annotations

import math
import os
import sys
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notifiers import (
    AlertKind,
    DiscordNotifier,
    WEBHOOK_RETRY_AFTER_CAP_S,
    reset_circuits,
)

TARGET_URL  = "https://example.com"
DISCORD_URL = "https://discord.com/api/webhooks/test/token"


@pytest.fixture(autouse=True)
def _fresh_circuits():
    """The breaker is module state; a test that trips it must not leak."""
    reset_circuits()
    yield
    reset_circuits()


@pytest.mark.parametrize(
    "header",
    [
        "nan",
        "NaN",
        "-nan",
        "inf",
        "-inf",
        "Infinity",
        "1e400",                          # overflows float() to inf
        "-5",
        "-0.5",
        "",
        "not-a-number",
        "Wed, 21 Oct 2026 07:28:00 GMT",  # HTTP-date form: ignored today, stays ignored
    ],
)
async def test_unusable_retry_after_falls_back_to_the_linear_backoff(
    monkeypatch, header: str
) -> None:
    """Only a finite, non-negative Retry-After may set the sleep. Anything else
    -- nan, inf, a negative, a date, junk -- is ignored and the normal backoff
    (1.0 s on the first retry) applies. The sleep argument is asserted finite
    as well as equal, so a future nan cannot hide behind an equality that nan
    never satisfies."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(DISCORD_URL, status=429, headers={"Retry-After": header})
            mock.post(DISCORD_URL, status=204)
            async with aiohttp.ClientSession() as session:
                delivered = await DiscordNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
        assert mock_sleep.call_count == 1
        (slept,), _ = mock_sleep.call_args
        assert math.isfinite(slept) and slept >= 0
        assert slept == 1.0
    assert delivered is True


@pytest.mark.parametrize("header, expected", [("0", 0.0), ("2.5", 2.5), ("30", 30.0)])
async def test_valid_retry_after_is_still_honoured(
    monkeypatch, header: str, expected: float
) -> None:
    """The guard must not eat the values it exists to keep: zero and the cap
    boundary are legal and are the server's stated wait."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(DISCORD_URL, status=429, headers={"Retry-After": header})
            mock.post(DISCORD_URL, status=204)
            async with aiohttp.ClientSession() as session:
                await DiscordNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
        mock_sleep.assert_called_once_with(expected)


async def test_retry_after_above_the_cap_is_clamped_not_ignored(monkeypatch) -> None:
    """Finite and huge is a real instruction, just too long to obey."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(DISCORD_URL, status=429, headers={"Retry-After": "86400"})
            mock.post(DISCORD_URL, status=204)
            async with aiohttp.ClientSession() as session:
                await DiscordNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
        mock_sleep.assert_called_once_with(WEBHOOK_RETRY_AFTER_CAP_S)
