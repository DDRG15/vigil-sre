"""
tests/test_sampler_isolation.py -- the connection sampler may degrade the
diagnostics of a probe. It may not fail the probe, and it may not fail the round.

check_url called host_port_from_url() and sample_connection() before the try
block that turns probe errors into a DOWN verdict. Two URLs that load fine from
YAML escaped it:

    http://example.com:99999/   urlsplit().port raises ValueError (port range)
    https://example..com/       the IDNA codec raises UnicodeError, which is a
                                ValueError and NOT an OSError, so the sampler's
                                own `except OSError` never saw it

Either one made gather() hand run_health_checks an exception instead of an
outcome. That counts as an internal error, withholds the heartbeat and exits 1,
and it does so on EVERY round for as long as the entry stays in the list: a
single typo takes the watchdog red permanently and costs the other targets'
"this round is complete" signal with it.

The probe itself had the same hole for the IDNA case: aiohttp lets the
UnicodeError out of its resolver instead of wrapping it in ClientError, so
_probe_once raised it past the retry loop. Fixing only the sampler left the
round exactly as broken.

Coverage:
  A. diagnostics: host_port_from_url / sample_connection do not raise
  B. check_url: a sampler failure degrades diagnostics, nothing more
  C. The probe: an unusable URL is a DOWN target, not a crashed check
  D. A whole round with such a URL in the list
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import (
    ConnectionSample,
    ProbePhases,
    host_port_from_url,
    sample_connection,
)
from main import Target

BAD_PORT  = "http://example.com:99999/"
BAD_LABEL = "https://example..com/"
BAD_IPV6  = "http://[::1/"
NOT_A_PORT = "http://example.com:abc/"


# =============================================================================
# A. diagnostics
# =============================================================================


@pytest.mark.parametrize("url", [BAD_PORT, BAD_IPV6, NOT_A_PORT])
def test_host_port_from_url_returns_none_for_a_url_it_cannot_split(url) -> None:
    assert host_port_from_url(url) is None


def test_host_port_from_url_still_splits_a_good_url() -> None:
    assert host_port_from_url("https://example.com/x") == ("example.com", 443)
    assert host_port_from_url("http://example.com:8080/") == ("example.com", 8080)
    assert host_port_from_url("http://example.com/") == ("example.com", 80)


@pytest.mark.parametrize("use_tls", [False, True])
async def test_sample_connection_returns_an_empty_sample_for_an_unencodable_host(
        use_tls) -> None:
    """'example..com' dies in the IDNA codec before any packet leaves: no
    network is involved, and nothing may escape."""
    sample = await sample_connection("example..com", 443, use_tls=use_tls)

    assert isinstance(sample, ConnectionSample)
    assert sample.rtt_ms is None and sample.tls_ms is None


@pytest.mark.parametrize("use_tls", [False, True])
async def test_sample_connection_returns_an_empty_sample_for_a_port_out_of_range(
        use_tls) -> None:
    sample = await sample_connection("127.0.0.1", 99999, use_tls=use_tls)

    assert isinstance(sample, ConnectionSample)
    assert sample.rtt_ms is None


# =============================================================================
# B. check_url
# =============================================================================


def _phases(url: str) -> ProbePhases:
    return ProbePhases(url=url, http_status=200, ttfb_ms=50.0, transfer_ms=1.0,
                       body_bytes=10)


@pytest.fixture
def probe_spy(monkeypatch):
    """The probe is stubbed (it is not what these tests are about); it records
    the sample it was handed."""
    seen: list = []

    async def _probe(session, url, sample=None, *a, **k):
        seen.append(sample)
        return _phases(url)

    monkeypatch.setattr(main, "_probe_with_backoff", _probe)
    monkeypatch.setattr(main, "dispatch_alert", AsyncMock(return_value=True))
    return seen


@pytest.mark.parametrize("url", [BAD_PORT, BAD_LABEL, BAD_IPV6, NOT_A_PORT])
async def test_check_url_survives_a_url_the_sampler_cannot_handle(
        tmp_path, probe_spy, url) -> None:
    """The real sampler, the real host_port_from_url. Before the fix this raised
    ValueError / UnicodeError out of check_url."""
    state = main.StateManager(tmp_path / "state.json")

    outcome = await main.check_url(url, None, state)

    assert outcome.status == main.STATUS_UP
    assert len(probe_spy) == 1, "the probe must still run"
    assert probe_spy[0] is None or probe_spy[0].rtt_ms is None, (
        "there is no usable sample for a URL that cannot be connected to")


@pytest.mark.parametrize("exc", [
    ValueError("port out of range"),
    UnicodeError("label empty or too long"),
    OSError("network unreachable"),
    asyncio.TimeoutError(),
])
async def test_check_url_degrades_to_no_sample_on_each_named_exception(
        tmp_path, monkeypatch, probe_spy, exc, caplog) -> None:
    async def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(main, "sample_connection", _boom)
    state = main.StateManager(tmp_path / "state.json")

    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        outcome = await main.check_url("https://ok.example/", None, state)

    assert outcome.status == main.STATUS_UP, "the probe verdict stands"
    assert probe_spy == [None], "the probe must be handed sample=None"
    warnings = [r.getMessage() for r in caplog.records
                if "sample" in r.getMessage().lower() and r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "https://ok.example/" in warnings[0]


async def test_a_bug_in_the_sampler_is_not_swallowed(tmp_path, monkeypatch, probe_spy) -> None:
    """Specific exceptions, not `except Exception`: a KeyError is a bug."""
    async def _bug(*a, **k):
        raise KeyError("a real bug")

    monkeypatch.setattr(main, "sample_connection", _bug)
    state = main.StateManager(tmp_path / "state.json")

    with pytest.raises(KeyError):
        await main.check_url("https://ok.example/", None, state)


# =============================================================================
# C. The probe
# =============================================================================


@pytest.mark.parametrize("url", [BAD_PORT, BAD_LABEL, BAD_IPV6])
async def test_an_unusable_url_is_a_probe_failure_not_a_crash(url) -> None:
    """aiohttp wraps most bad URLs in ClientError but lets the IDNA UnicodeError
    out of its resolver. A probe failure is an answer (DOWN); a raw exception
    is a check that produced none."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        with pytest.raises(main._ProbeFailure):
            await main._probe_once(session, url)


@pytest.mark.parametrize("url", [BAD_PORT, BAD_LABEL])
async def test_check_url_reports_an_unusable_url_as_down(tmp_path, monkeypatch, url) -> None:
    import aiohttp

    monkeypatch.setattr(main, "RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(main, "dispatch_alert", AsyncMock(return_value=True))
    state = main.StateManager(tmp_path / "state.json")

    async with aiohttp.ClientSession() as session:
        outcome = await main.check_url(url, session, state)

    assert outcome.status == main.STATUS_DOWN
    assert outcome.error


# =============================================================================
# D. A whole round
# =============================================================================


async def test_a_round_with_unusable_urls_in_the_list_is_still_a_complete_round(
        tmp_path, monkeypatch) -> None:
    """The consequence: internal_errors, no heartbeat, exit 1 -- every minute,
    from one typo. Now it is two DOWN targets and a complete round."""
    monkeypatch.setattr(main, "RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(main, "dispatch_alert", AsyncMock(return_value=True))
    heartbeat = AsyncMock()
    monkeypatch.setattr(main, "_send_heartbeat", heartbeat)
    state_path = tmp_path / "state.json"
    report = main.RunReport()

    down = await main.run_health_checks(
        targets=[Target(url=BAD_PORT), Target(url=BAD_LABEL)],
        state_path=state_path, history_path=tmp_path / "h.db", report=report)

    assert report.internal_errors == 0, "a bad URL crashed its check"
    assert down == 2
    assert heartbeat.await_count == 1
    assert main._finish_run(False, down, state_path, report) == 0
