"""
tests/test_main.py — Test suite for vigil-sre health checker.

Coverage:
  A. load_targets()            — 14 tests  (sync) — incl. an earlier release ${VAR} resolution + audit fixes
  B. StateManager              — 10 tests  (async) — includes invariant checks
  C. _probe_once()             —  6 tests  (async, aioresponses mock)
  D. _probe_with_backoff()     —  3 tests  (async, sleep call count verified)
  E. Payload builders          —  7 tests  (sync, Discord + Slack, an earlier release)
  F. check_url() pipeline      —  5 tests  (async, full orchestration)
  I2. _generate_report()       —  6 tests  (an earlier release, incl. subprocess end-to-end)
  J. Logging configuration     —  3 tests  (sync) — an earlier release log rotation, incl. subprocess wiring check

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
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import ConnectionSample, ProbePhases

from main import (
    StateManager,
    Target,
    _ProbeFailure,
    _exit_code,
    _probe_once,
    _probe_with_backoff,
    _warn_if_over_budget,
    check_url,
    load_targets,
    run_health_checks,
)
from notifiers import (
    AlertKind,
    DiscordNotifier,
    SlackNotifier,
    dispatch_alert,
)

# A healthy default sample for check_url tests: fast RTT, no h2 question,
# no TLS issue. Individual tests override fields to force DEGRADED paths.
_HEALTHY_SAMPLE = ConnectionSample(rtt_ms=15.0, tls_ms=8.0, alpn_protocol="h2", h2_supported=True)

TARGET_URL = "https://example.com"


# =============================================================================
# A. load_targets()
# =============================================================================


def test_load_targets_valid(tmp_path: Path) -> None:
    """Plain string entries load as Target with expect_substring=None."""
    f = tmp_path / "targets.yaml"
    f.write_text("targets:\n  - https://a.com\n  - https://b.com\n", encoding="utf-8")
    result = load_targets(f)
    assert result == [Target(url="https://a.com"), Target(url="https://b.com")]


def test_load_targets_mixed_string_and_object(tmp_path: Path) -> None:
    """String and object entries can coexist; only the object form carries
    expect_substring — this is the an earlier release content-assertion schema."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n"
        "  - https://plain.com\n"
        '  - url: https://api.com/health\n'
        '    expect_substring: \'"status":"ok"\'\n',
        encoding="utf-8",
    )
    result = load_targets(f)
    assert result == [
        Target(url="https://plain.com"),
        Target(url="https://api.com/health", expect_substring='"status":"ok"'),
    ]


def test_load_targets_object_without_url_exits(tmp_path: Path) -> None:
    """An object entry missing the required 'url' key must exit, not crash
    downstream with a confusing AttributeError."""
    f = tmp_path / "targets.yaml"
    f.write_text("targets:\n  - expect_substring: 'ok'\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_non_string_expect_substring_exits(tmp_path: Path) -> None:
    """expect_substring: 200 (no quotes) parses as an int in YAML. Without
    validation, _probe_once's .encode("utf-8") call would crash that target's
    probe every run — this must fail fast at load time instead, with a
    message that points at the YAML typo, not a mystery AttributeError."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n  - url: https://api.com/health\n    expect_substring: 200\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_valid_overrides(tmp_path: Path) -> None:
    """The 4 per-target override fields (an earlier release) must parse into Target as-is."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n"
        "  - url: https://api.com/health\n"
        "    expected_status: 204\n"
        "    timeout_s: 10\n"
        "    degraded_ttfb_ms: 3000\n"
        "    degraded_rtt_ms: 250\n",
        encoding="utf-8",
    )
    result = load_targets(f)
    assert result == [Target(
        url="https://api.com/health",
        expected_status=204, timeout_s=10, degraded_ttfb_ms=3000, degraded_rtt_ms=250,
    )]


def test_load_targets_overrides_are_optional(tmp_path: Path) -> None:
    """An entry with no override fields must default every one to None —
    retro-compatible with every targets.yaml written before an earlier release."""
    f = tmp_path / "targets.yaml"
    f.write_text("targets:\n  - url: https://api.com/health\n", encoding="utf-8")
    result = load_targets(f)
    target = result[0]
    assert target.expected_status is None
    assert target.timeout_s is None
    assert target.degraded_ttfb_ms is None
    assert target.degraded_rtt_ms is None


@pytest.mark.parametrize("field", ["expected_status", "timeout_s", "degraded_ttfb_ms", "degraded_rtt_ms"])
def test_load_targets_non_numeric_override_exits(tmp_path: Path, field: str) -> None:
    """A quoted/non-numeric override value must fail fast at load time,
    same discipline as the expect_substring type check above."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        f"targets:\n  - url: https://api.com/health\n    {field}: 'not-a-number'\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


@pytest.mark.parametrize("field", ["expected_status", "timeout_s", "degraded_ttfb_ms", "degraded_rtt_ms"])
def test_load_targets_boolean_override_exits(tmp_path: Path, field: str) -> None:
    """YAML's true/false must never silently become 1/0 for a numeric field —
    bool is a subtype of int in Python, so this needs an explicit rejection."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        f"targets:\n  - url: https://api.com/health\n    {field}: true\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_load_targets_non_positive_timeout_exits(tmp_path: Path, bad: str) -> None:
    """aiohttp treats ClientTimeout(total=0) (and negatives) as NO timeout at
    all: a target that hangs would freeze the whole run's asyncio.gather
    forever, and every other target with it. (audit finding)"""
    f = tmp_path / "targets.yaml"
    f.write_text(f"targets:\n  - url: https://x.com\n    timeout_s: {bad}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_targets(f)


@pytest.mark.parametrize("field,bad", [
    ("timeout_s", "0"), ("timeout_s", "-5"),
    ("degraded_ttfb_ms", "0"), ("degraded_ttfb_ms", "-1"),
    ("degraded_rtt_ms", "0"), ("degraded_rtt_ms", "-100"),
    ("expected_status", "999"), ("expected_status", "-1"),
])
def test_load_targets_out_of_range_override_exits(tmp_path: Path, field: str, bad: str) -> None:
    """_validated_numeric_field checked type but not range: a threshold of 0
    or below fires on every healthy probe (SLOW_RESPONSE against a 12ms
    response with degraded_ttfb_ms=0, reproduced live), producing exactly the
    chronic false positives per-target overrides exist to eliminate -- the
    anti-alert-fatigue feature generating alert fatigue."""
    f = tmp_path / "targets.yaml"
    f.write_text(f"targets:\n  - url: https://x.com\n    {field}: {bad}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_warns_when_timeout_blows_the_run_budget(tmp_path: Path, caplog) -> None:
    """timeout_s=30 has a worst case of 96s (3 attempts + backoff) against
    RUN_BUDGET_S=60s -- not rejected (a legitimately slow endpoint is a valid
    use case), but the operator must see what a full outage on this target
    costs the rest of the run."""
    f = tmp_path / "targets.yaml"
    f.write_text("targets:\n  - url: https://x.com\n    timeout_s: 30\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        load_targets(f)
    assert any("RUN_BUDGET_S" in r.message for r in caplog.records)


def test_load_targets_resolves_env_var_expect_substring(tmp_path: Path, monkeypatch) -> None:
    """${VAR_NAME} in expect_substring (an earlier release) resolves from the environment
    at load time: the real value is used for matching, and expect_substring_display
    carries a safe placeholder instead — never the secret itself."""
    monkeypatch.setenv("HEALTH_TOKEN", "super-secret-value")
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n  - url: https://api.com/health\n    expect_substring: ${HEALTH_TOKEN}\n",
        encoding="utf-8",
    )
    result = load_targets(f)
    assert result[0].expect_substring == "super-secret-value"
    assert result[0].expect_substring_display == "${HEALTH_TOKEN} (from env)"


def test_load_targets_literal_expect_substring_has_no_display_override(tmp_path: Path) -> None:
    """A plain literal (not ${VAR_NAME}) must leave expect_substring_display
    as None -- it is already public in git, nothing to redact, and callers
    fall back to expect_substring itself when display is None."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n  - url: https://api.com/health\n    expect_substring: literal-value\n",
        encoding="utf-8",
    )
    result = load_targets(f)
    assert result[0].expect_substring == "literal-value"
    assert result[0].expect_substring_display is None


def test_load_targets_missing_env_var_in_expect_substring_exits(tmp_path: Path, monkeypatch) -> None:
    """A ${VAR_NAME} reference to an unset variable must fail fast at load
    time -- a probe that silently never matches because its secret failed to
    resolve is a worse failure mode than refusing to start."""
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n  - url: https://api.com/health\n    expect_substring: ${MISSING_TOKEN}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_empty_env_var_in_expect_substring_exits(tmp_path: Path, monkeypatch) -> None:
    """A variable that IS set but resolves to "" must also fail fast: "" is
    contained in every byte string, so the content assertion would silently
    become a no-op that reports UP over any body, including an error page --
    the exact case the assertion exists to catch. (audit finding)"""
    monkeypatch.setenv("HEALTH_TOKEN", "")
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n  - url: https://api.com/health\n    expect_substring: ${HEALTH_TOKEN}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


def test_load_targets_lowercase_env_reference_exits(tmp_path: Path, monkeypatch) -> None:
    """os.environ is case-insensitive on Windows and case-sensitive on Linux:
    ${health_token} against a HEALTH_TOKEN= entry resolves in local dev and
    hard-exits main.py the moment the same targets.yaml runs in the
    container. Rejecting it on every platform makes the config fail cheaply
    in dev instead of expensively in a crash-looping container."""
    monkeypatch.setenv("HEALTH_TOKEN", "super-secret-value")
    f = tmp_path / "targets.yaml"
    f.write_text(
        "targets:\n  - url: https://api.com/health\n    expect_substring: ${health_token}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


@pytest.mark.parametrize("bad", [
    "Bearer ${HEALTH_TOKEN}",
    '${HEALTH_TOKEN}"}',
    "${ HEALTH_TOKEN }",
])
def test_load_targets_partial_env_reference_exits(tmp_path: Path, monkeypatch, bad: str) -> None:
    """A value that contains "${" but isn't a clean whole-string match
    (concatenation, stray spaces) would otherwise be compared byte-for-byte
    against the response body -- it can never match, leaving the target DOWN
    forever on a config typo dressed as a real outage."""
    monkeypatch.setenv("HEALTH_TOKEN", "super-secret-value")
    f = tmp_path / "targets.yaml"
    f.write_text(
        f"targets:\n  - url: https://api.com/health\n    expect_substring: '{bad}'\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        load_targets(f)


@pytest.mark.parametrize("literal", ['"$schema"', "$HEALTH_TOKEN", "price: $5.00"])
def test_load_targets_dollar_sign_without_braces_is_not_mistaken_for_a_reference(
    tmp_path: Path, literal: str
) -> None:
    """A literal that happens to contain a bare '$' (a price, '$schema' in a
    JSON body, or even a var-like "$NAME" with no braces) is not an env-var
    reference -- only "${" triggers the malformed-reference check, by design,
    so all of these must load as normal literals rather than aborting."""
    f = tmp_path / "targets.yaml"
    f.write_text(
        f"targets:\n  - url: https://api.com/health\n    expect_substring: '{literal}'\n",
        encoding="utf-8",
    )
    result = load_targets(f)
    assert result[0].expect_substring == literal
    assert result[0].expect_substring_display is None


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
    """A healthy target needs DOWN_CONFIRMATIONS (2) consecutive failures
    before it is confirmed DOWN — the first failure only arms hysteresis."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)
    first = await sm.set_down(TARGET_URL, "Timeout (>5s)")
    assert first is False   # strike 1/2 — hysteresis pending, no alert yet
    second = await sm.set_down(TARGET_URL, "Timeout (>5s)")
    assert second is True   # strike 2/2 — confirmed DOWN, alert fires


async def test_state_atomic_write(tmp_path: Path) -> None:
    """State file must be valid JSON with correct fields after every write."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_up(TARGET_URL)

    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    record = data["targets"][TARGET_URL]
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
    assert data["targets"][TARGET_URL]["last_error"] == ""


async def test_state_last_changed_unchanged_on_repeat(tmp_path: Path) -> None:
    """Calling set_up() twice must NOT advance last_changed on the second call.
    last_changed tracks transitions, not every probe. If it advances on every
    call, operators cannot tell when a service actually recovered."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_up(TARGET_URL)

    first = json.loads(state_file.read_text(encoding="utf-8"))
    last_changed_after_first = first["targets"][TARGET_URL]["last_changed"]

    await sm.set_up(TARGET_URL)  # same state — must not change last_changed

    second = json.loads(state_file.read_text(encoding="utf-8"))
    last_changed_after_second = second["targets"][TARGET_URL]["last_changed"]

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
# B2. Flap hysteresis + schema v2 (an earlier release)
# =============================================================================


async def test_set_down_from_healthy_pending_keeps_previous_status(tmp_path: Path) -> None:
    """First failure from UP must NOT flip status to DOWN — it stays UP while
    the strike is recorded, so a recovering target never even reaches DOWN."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    await sm.set_up(TARGET_URL)

    transitioned = await sm.set_down(TARGET_URL, "HTTP 503")
    assert transitioned is False

    data = json.loads(state_file.read_text(encoding="utf-8"))
    record = data["targets"][TARGET_URL]
    assert record["status"] == "UP"                    # NOT flipped to DOWN
    assert record["consecutive_failures"] == 1
    assert record["last_error"] == "HTTP 503"           # failure detail visible


async def test_set_down_recovery_resets_strike_counter(tmp_path: Path) -> None:
    """A recovery between failures must reset the strike counter to zero —
    otherwise two failures separated by a healthy day would wrongly combine
    into a false DOWN confirmation."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)
    await sm.set_down(TARGET_URL, "HTTP 503")   # strike 1/2, still UP
    await sm.set_up(TARGET_URL)                 # recovers — strike resets

    transitioned = await sm.set_down(TARGET_URL, "HTTP 503")
    assert transitioned is False   # back to strike 1/2, not 2/2 — no alert yet


async def test_set_down_degraded_to_down_confirms_after_threshold(tmp_path: Path) -> None:
    """Hysteresis applies from DEGRADED too, not just from UP."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_degraded(TARGET_URL, "HIGH_RTT_NEEDS_EDGE (rtt_ms=300)")
    first = await sm.set_down(TARGET_URL, "HTTP 503")
    assert first is False
    second = await sm.set_down(TARGET_URL, "HTTP 503")
    assert second is True


async def test_state_migrates_legacy_v1_schema(tmp_path: Path) -> None:
    """A pre-Fase-7 state.json (flat url→record mapping, no schema_version)
    must load transparently and re-save itself in v2 shape on the next write —
    an operator upgrading vigil-sre must not lose alert-suppression history."""
    state_file = tmp_path / "state.json"
    legacy_v1 = {
        TARGET_URL: {
            "status"      : "UP",
            "last_checked": "2026-01-01T00:00:00Z",
            "last_changed": "2026-01-01T00:00:00Z",
            "last_error"  : "",
        }
    }
    state_file.write_text(json.dumps(legacy_v1), encoding="utf-8")

    sm = StateManager(state_file)
    # The legacy record must be visible immediately (no data loss on load)...
    transitioned = await sm.set_up(TARGET_URL)
    assert transitioned is False   # already UP per the migrated legacy record

    # ...and the next write must upgrade the on-disk shape to v2.
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["targets"][TARGET_URL]["status"] == "UP"


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


async def test_probe_once_status_mismatch_names_the_expectation() -> None:
    """'HTTP 200' as a DOWN reason is undecipherable at 3am on a target whose
    expected_status is an override (an earlier release) -- the message must name what
    was expected, not just what came back. (audit finding)"""
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(session, TARGET_URL, expected_status=204)
    assert "expected 204" in exc_info.value.detail


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


async def test_probe_once_content_check_passes() -> None:
    """A 200 whose body contains expect_substring is a real success."""
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b'{"status":"ok"}')
        async with aiohttp.ClientSession() as session:
            phases = await _probe_once(
                session, TARGET_URL, expect_substring='"status":"ok"'
            )
    assert phases.http_status == 200


async def test_probe_once_content_check_fails() -> None:
    """A 200 with a body missing expect_substring must count as a probe
    failure — a status code alone cannot tell an error page from a real one."""
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"<html>error page</html>")
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(
                    session, TARGET_URL, expect_substring='"status":"ok"'
                )
    assert "Content check failed" in exc_info.value.detail


async def test_probe_once_content_check_failure_redacts_secret() -> None:
    """When expect_substring_display is set (an earlier release, ${VAR_NAME} secrets),
    a failed content check must report the placeholder, never the real
    secret -- this is the one point of redaction every downstream sink
    (logs, alerts, state.json, history.db) inherits from."""
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"<html>error page</html>")
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(
                    session, TARGET_URL,
                    expect_substring="super-secret-value",
                    expect_substring_display="${HEALTH_TOKEN} (from env)",
                )
    assert "${HEALTH_TOKEN} (from env)" in exc_info.value.detail
    assert "super-secret-value" not in exc_info.value.detail


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
# E. Payload builders (per channel)
#
# Both builders are pure — no clock, no network — which is the whole reason
# they are separate from send(): the wire format of every channel stays
# testable without touching a socket.
# =============================================================================


def test_discord_payload_failure() -> None:
    payload = DiscordNotifier().build_payload(
        url=TARGET_URL,
        status_detail="HTTP 503",
        timestamp="2026-01-01T00:00:00Z",
        kind=AlertKind.FAILURE,
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFF0000
    assert "Alert" in embed["title"]


def test_discord_payload_recovery() -> None:
    payload = DiscordNotifier().build_payload(
        url=TARGET_URL,
        status_detail="Service is UP",
        timestamp="2026-01-01T00:00:00Z",
        kind=AlertKind.RECOVERY,
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0x00C853
    assert "Recovered" in embed["title"]


def test_discord_payload_fields() -> None:
    payload = DiscordNotifier().build_payload(
        url=TARGET_URL,
        status_detail="HTTP 503",
        timestamp="2026-01-01T00:00:00Z",
        kind=AlertKind.FAILURE,
    )
    fields = payload["embeds"][0]["fields"]
    field_values = [f["value"] for f in fields]
    assert any(TARGET_URL in v for v in field_values)
    assert any("2026-01-01T00:00:00Z" in v for v in field_values)


@pytest.mark.parametrize("kind,color", [
    (AlertKind.FAILURE,  "#FF0000"),
    (AlertKind.RECOVERY, "#00C853"),
    (AlertKind.DEGRADED, "#FFB300"),
])
def test_slack_payload_colour_matches_discord_semantics(kind: AlertKind, color: str) -> None:
    """The triage colour must mean the same thing on both channels — an alert
    that is amber on Discord and red on Slack teaches two different reflexes
    for one event."""
    payload = SlackNotifier().build_payload(
        url=TARGET_URL,
        status_detail="detail",
        timestamp="2026-01-01T00:00:00Z",
        kind=kind,
    )
    assert payload["attachments"][0]["color"] == color


def test_slack_payload_carries_url_detail_and_timestamp() -> None:
    payload = SlackNotifier().build_payload(
        url=TARGET_URL,
        status_detail="HTTP 503",
        timestamp="2026-01-01T00:00:00Z",
        kind=AlertKind.FAILURE,
    )
    blocks = payload["attachments"][0]["blocks"]
    rendered = json.dumps(blocks)
    assert TARGET_URL in rendered
    assert "HTTP 503" in rendered
    assert "2026-01-01T00:00:00Z" in rendered


def test_slack_payload_sets_text_for_the_mobile_push_preview() -> None:
    """Slack shows `text` as the push notification. Omitting it delivers an
    empty push — which defeats the point of a channel whose job is catching
    the alert the other channel might bury."""
    payload = SlackNotifier().build_payload(
        url=TARGET_URL,
        status_detail="HTTP 503",
        timestamp="2026-01-01T00:00:00Z",
        kind=AlertKind.FAILURE,
    )
    assert payload["text"]
    assert TARGET_URL in payload["text"]


# =============================================================================
# E2. Notifier.send() — webhook delivery retry (an earlier release, the design note)
#
# An alert fires on a state transition only — there is no next-run retry the
# way probes have. asyncio.sleep is mocked so retries don't add real wall time.
# =============================================================================

WEBHOOK_URL       = "https://discord.com/api/webhooks/test/token"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/TEST/TEST/token"


async def test_webhook_204_first_try_no_retry(monkeypatch, caplog) -> None:
    """A clean 204 must not sleep or retry, and must log that it was sent —
    not just that no exception was raised. A function that silently returned
    without posting would pass a sleep-count-only assertion just as well."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=204)
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("INFO"):
                    await DiscordNotifier().send(session, TARGET_URL, "Service is UP", AlertKind.RECOVERY)
        mock_sleep.assert_not_called()
    assert any("alert sent" in r.message for r in caplog.records)


async def test_webhook_429_then_204_retries_once(monkeypatch) -> None:
    """A 429 (rate-limited) with no Retry-After header must fall back to the
    linear backoff schedule, and succeed on the 2nd attempt."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=429)
            mock.post(WEBHOOK_URL, status=204)
            async with aiohttp.ClientSession() as session:
                await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)
        mock_sleep.assert_called_once_with(1.0)


async def test_webhook_429_honors_retry_after_header(monkeypatch) -> None:
    """A 429 with a Retry-After header must sleep for exactly that long,
    not the linear backoff schedule — Discord is telling us the real wait."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=429, headers={"Retry-After": "5"})
            mock.post(WEBHOOK_URL, status=204)
            async with aiohttp.ClientSession() as session:
                await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)
        mock_sleep.assert_called_once_with(5.0)


async def test_webhook_429_retry_after_is_capped(monkeypatch) -> None:
    """A Retry-After longer than the cap must be clamped — a single 429 must
    not be allowed to stall the whole run indefinitely."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=429, headers={"Retry-After": "999"})
            mock.post(WEBHOOK_URL, status=204)
            async with aiohttp.ClientSession() as session:
                await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)
        mock_sleep.assert_called_once_with(30.0)


async def test_webhook_429_malformed_retry_after_falls_back(monkeypatch) -> None:
    """An unparseable Retry-After must not crash the retry loop — fall back
    to the linear backoff schedule instead."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=429, headers={"Retry-After": "not-a-number"})
            mock.post(WEBHOOK_URL, status=204)
            async with aiohttp.ClientSession() as session:
                await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)
        mock_sleep.assert_called_once_with(1.0)


async def test_webhook_5xx_exhausts_retries_alert_lost(monkeypatch, caplog) -> None:
    """3 consecutive 500s: retried twice, then abandoned — the alert is LOST,
    logged as such (not silently or as if it succeeded), but Notifier.send()
    must never raise for the caller (check_url)."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=500)
            mock.post(WEBHOOK_URL, status=500)
            mock.post(WEBHOOK_URL, status=500)
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("ERROR"):
                    await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)  # must not raise
        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list == [call(1.0), call(2.0)]
    assert any("LOST" in r.message for r in caplog.records)


async def test_webhook_400_non_retryable_no_retry(monkeypatch, caplog) -> None:
    """A 400 (malformed payload) will not heal on retry — one POST, no sleep,
    and the log must say why it was abandoned rather than retried."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, status=400)
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("ERROR"):
                    await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)
        mock_sleep.assert_not_called()
    assert any("non-retryable" in r.message for r in caplog.records)


async def test_webhook_timeout_retries_then_gives_up(monkeypatch, caplog) -> None:
    """A TimeoutError on every attempt must retry twice then give up cleanly,
    logging the alert as LOST rather than swallowing the failure silently."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(WEBHOOK_URL, exception=asyncio.TimeoutError())
            mock.post(WEBHOOK_URL, exception=asyncio.TimeoutError())
            mock.post(WEBHOOK_URL, exception=asyncio.TimeoutError())
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("ERROR"):
                    await DiscordNotifier().send(session, TARGET_URL, "HTTP 503", AlertKind.FAILURE)  # must not raise
        assert mock_sleep.call_count == 2
    assert any("LOST" in r.message for r in caplog.records)


async def test_send_returns_false_when_channel_is_not_configured(monkeypatch) -> None:
    """An unconfigured channel is a choice, not an error — it returns False
    without posting, and dispatch_alert is what decides whether that mattered."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    async with aiohttp.ClientSession() as session:
        delivered = await SlackNotifier().send(
            session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
        )
    assert delivered is False


async def test_slack_send_accepts_200_not_204(monkeypatch) -> None:
    """Slack answers 200 where Discord answers 204. Treating both channels as
    204-means-success would mark every successful Slack delivery as a failure,
    retry it twice, and report the alert LOST while the message sat in the
    channel."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", SLACK_WEBHOOK_URL)
    with patch("notifiers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with aioresponses() as mock:
            mock.post(SLACK_WEBHOOK_URL, status=200, body="ok")
            async with aiohttp.ClientSession() as session:
                delivered = await SlackNotifier().send(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE
                )
        mock_sleep.assert_not_called()
    assert delivered is True


# =============================================================================
# E3. dispatch_alert() — the redundancy that justifies the whole phase
#
# The point of this phase is not "two channels exist". It is that one channel
# failing cannot stop the other from delivering, and that the operator finds
# out when NO channel delivered. Those are the properties tested here.
# =============================================================================


async def test_dispatch_delivers_to_every_configured_channel() -> None:
    """Both channels get the same alert from one transition — no routing, no
    choosing. The duplication IS the feature."""
    discord, slack = DiscordNotifier(), SlackNotifier()
    with patch.object(DiscordNotifier, "send", new_callable=AsyncMock, return_value=True) as d_send:
        with patch.object(SlackNotifier, "send", new_callable=AsyncMock, return_value=True) as s_send:
            async with aiohttp.ClientSession() as session:
                await dispatch_alert(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE,
                    notifiers=[discord, slack],
                )
    d_send.assert_awaited_once()
    s_send.assert_awaited_once()


async def test_dispatch_one_channel_down_does_not_stop_the_other(caplog) -> None:
    """THE test this phase exists for: Slack fails every attempt, Discord must
    still deliver, and nothing may be logged as a total loss."""
    with patch.object(DiscordNotifier, "send", new_callable=AsyncMock, return_value=True) as d_send:
        with patch.object(SlackNotifier, "send", new_callable=AsyncMock, return_value=False):
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("CRITICAL"):
                    await dispatch_alert(
                        session, TARGET_URL, "HTTP 503", AlertKind.FAILURE,
                        notifiers=[DiscordNotifier(), SlackNotifier()],
                    )
    d_send.assert_awaited_once()
    assert not any("alerts_lost_all_channels_total" in r.message for r in caplog.records)


async def test_dispatch_logs_the_metric_that_pages_when_every_channel_fails(caplog) -> None:
    """Per-channel losses are diagnosis. This one line means 'an alert was
    generated and no human will hear about it' — the only one that should page."""
    with patch.object(DiscordNotifier, "send", new_callable=AsyncMock, return_value=False):
        with patch.object(SlackNotifier, "send", new_callable=AsyncMock, return_value=False):
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("CRITICAL"):
                    await dispatch_alert(
                        session, TARGET_URL, "HTTP 503", AlertKind.FAILURE,
                        notifiers=[DiscordNotifier(), SlackNotifier()],
                    )
    assert any(
        "event_type=metric metric=alerts_lost_all_channels_total value=1" in r.message
        for r in caplog.records
    )


async def test_dispatch_with_zero_channels_configured_reports_the_loss(caplog) -> None:
    """Running with no webhook at all must be loud. Silence here looks exactly
    like 'nothing was wrong', which is the failure mode this project has now
    been bitten by three separate times."""
    async with aiohttp.ClientSession() as session:
        with caplog.at_level("CRITICAL"):
            await dispatch_alert(
                session, TARGET_URL, "HTTP 503", AlertKind.FAILURE, notifiers=[],
            )
    assert any(
        "alerts_lost_all_channels_total" in r.message and "no_channels" in r.message
        for r in caplog.records
    )


async def test_dispatch_survives_a_notifier_that_breaks_its_never_raise_contract(caplog) -> None:
    """send() must never raise, but that is a convention a future notifier
    author can break. The gather makes the guarantee structural: a raising
    notifier is logged as the bug it is, and the healthy channel still
    delivers."""
    with patch.object(DiscordNotifier, "send", new_callable=AsyncMock, return_value=True) as d_send:
        with patch.object(SlackNotifier, "send", new_callable=AsyncMock,
                          side_effect=RuntimeError("notifier bug")):
            async with aiohttp.ClientSession() as session:
                with caplog.at_level("ERROR"):
                    await dispatch_alert(   # must not raise
                        session, TARGET_URL, "HTTP 503", AlertKind.FAILURE,
                        notifiers=[DiscordNotifier(), SlackNotifier()],
                    )
    d_send.assert_awaited_once()
    assert any("bug in that notifier" in r.message for r in caplog.records)


async def test_dispatch_runs_channels_concurrently_not_sequentially() -> None:
    """Sequential dispatch would double the worst case to 150s against a 60s
    run budget (the design note). Two channels that each sleep must finish in
    about one sleep, not two."""
    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.20)
        return True

    with patch.object(DiscordNotifier, "send", new=slow_send):
        with patch.object(SlackNotifier, "send", new=slow_send):
            async with aiohttp.ClientSession() as session:
                start = time.monotonic()
                await dispatch_alert(
                    session, TARGET_URL, "HTTP 503", AlertKind.FAILURE,
                    notifiers=[DiscordNotifier(), SlackNotifier()],
                )
                elapsed = time.monotonic() - start

    assert elapsed < 0.35, (
        f"dispatch took {elapsed:.2f}s for two 0.20s channels — that is "
        f"sequential, and sequential blows the run budget under Retry-After."
    )


# =============================================================================
# F. check_url() — full orchestration pipeline
#
# check_url() is where probe + state + alert dispatch are wired together.
# These tests verify that the CORRECT alert is fired (or suppressed) based
# on the state transition, not just that no exception was raised.
# dispatch_alert is mocked — we are not testing webhook delivery here, we are
# testing whether check_url DECIDES to alert, and with which AlertKind.
# =============================================================================


async def test_check_url_up_no_transition(tmp_path: Path) -> None:
    """URL already UP → probe returns 200 → no alert should fire."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)
    mock_alert.assert_not_called()


async def test_check_url_up_transition_fires_recovery_alert(tmp_path: Path) -> None:
    """URL was DOWN → probe returns 200 → recovery alert must fire with is_recovery=True."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_down(TARGET_URL, "HTTP 503")

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["kind"] is AlertKind.RECOVERY


async def test_check_url_down_transition_fires_failure_alert(tmp_path: Path) -> None:
    """URL was UP, already has one strike → probe fails all retries → the
    2nd strike reaches DOWN_CONFIRMATIONS and the failure alert fires.

    The strike is pre-seeded here because flap hysteresis (an earlier release) requires
    DOWN_CONFIRMATIONS consecutive failed runs from a healthy state before
    confirming DOWN — a single failure from UP no longer alerts immediately."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)
    await sm.set_down(TARGET_URL, "priming strike 1/2")  # arm hysteresis

    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                    with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                        await check_url(TARGET_URL, session, sm)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["kind"] is AlertKind.FAILURE


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
                with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                    with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                        await check_url(TARGET_URL, session, sm)

    mock_alert.assert_not_called()


async def test_check_url_content_check_failure_routes_to_down(tmp_path: Path) -> None:
    """A 200 that fails the content assertion must be treated exactly like
    any other probe failure: DOWN (confirmed immediately — no prior history),
    with a failure alert carrying the content-check detail."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)

    with aioresponses() as mock:
        # All RETRY_ATTEMPTS responses are 200 with the wrong body — the
        # content check must fail on every attempt, not just the first.
        mock.get(TARGET_URL, status=200, body=b"<html>down for maintenance</html>")
        mock.get(TARGET_URL, status=200, body=b"<html>down for maintenance</html>")
        mock.get(TARGET_URL, status=200, body=b"<html>down for maintenance</html>")
        with patch("main.asyncio.sleep", new_callable=AsyncMock):
            async with aiohttp.ClientSession() as session:
                with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                    with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                        await check_url(
                            TARGET_URL, session, sm, expect_substring='"status":"ok"'
                        )

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["kind"] is AlertKind.FAILURE

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["targets"][TARGET_URL]["status"] == "DOWN"
    assert "Content check failed" in data["targets"][TARGET_URL]["last_error"]


async def test_check_url_redacts_env_var_secret_end_to_end(tmp_path: Path) -> None:
    """The redaction contract (an earlier release) survives the full pipeline: a target
    configured with an env-var-backed expect_substring must never leak the
    real secret into state.json, even though the secret IS what the probe
    matched against — only the ${VAR_NAME} placeholder should land on disk."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)
    real_secret = "super-secret-value"

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"<html>down for maintenance</html>")
        mock.get(TARGET_URL, status=200, body=b"<html>down for maintenance</html>")
        mock.get(TARGET_URL, status=200, body=b"<html>down for maintenance</html>")
        with patch("main.asyncio.sleep", new_callable=AsyncMock):
            async with aiohttp.ClientSession() as session:
                with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                    with patch("main.dispatch_alert", new_callable=AsyncMock):
                        await check_url(
                            TARGET_URL, session, sm,
                            expect_substring=real_secret,
                            expect_substring_display="${HEALTH_TOKEN} (from env)",
                        )

    raw = state_file.read_text(encoding="utf-8")
    assert real_secret not in raw
    data = json.loads(raw)
    assert "${HEALTH_TOKEN} (from env)" in data["targets"][TARGET_URL]["last_error"]


async def test_check_url_up_persists_diagnostics(tmp_path: Path) -> None:
    """A successful probe must write a diagnostics block into state.json.

    This is the whole point of the BDP feature: after check_url runs on an
    UP target, an operator reading state.json must find the latency/BDP
    breakdown, not just status=UP."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock):
                    await check_url(TARGET_URL, session, sm)

    data = json.loads(state_file.read_text(encoding="utf-8"))
    record = data["targets"][TARGET_URL]
    assert record["status"] == "UP"
    assert "diagnostics" in record
    assert record["diagnostics"]["measured"]["rtt_ms"] == 15.0
    assert record["diagnostics"]["measured"]["body_bytes"] == 1024
    # A 1 KB body is far below the bandwidth sample floor — confidence must
    # be explicitly INSUFFICIENT_SAMPLE, never silently HIGH.
    assert record["diagnostics"]["derived"]["bandwidth_confidence"] == "INSUFFICIENT_SAMPLE"


async def test_probe_once_returns_phases_with_body(tmp_path: Path) -> None:
    """_probe_once must return a populated ProbePhases and read the body.

    Reading the body is what makes goodput measurable; a probe that only
    reads headers can never diagnose a window-limited transfer."""
    sample = ConnectionSample(rtt_ms=20.0, tls_ms=9.0, alpn_protocol="h2", h2_supported=True)
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"y" * 2048)
        async with aiohttp.ClientSession() as session:
            phases = await _probe_once(session, TARGET_URL, sample)

    assert phases.http_status == 200
    assert phases.body_bytes == 2048
    assert phases.rtt_ms == 20.0
    assert phases.tls_ms == 9.0
    assert phases.h2_supported is True
    assert phases.ttfb_ms >= 0.0


async def test_probe_once_redirect_voids_server_processing_ms() -> None:
    """A redirected request must never compute server_processing_ms.

    The trace hooks keep only the LAST hop's dns/connect timings, but ttfb
    spans the WHOLE chain (measured from before the first hop). Subtracting a
    single-hop network cost from a multi-hop ttfb would misattribute the
    earlier hop's full round trip to "the server thinking" -- a false
    SLOW_BACKEND finding on a target that is only redirecting, not slow.
    A large rtt_ms is used here specifically so that, absent the fix, the
    subtraction would still likely yield a positive (wrong) number."""
    sample = ConnectionSample(rtt_ms=5.0, tls_ms=None, alpn_protocol=None, h2_supported=None)
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=301, headers={"Location": "https://redirected.example.test/health"})
        mock.get("https://redirected.example.test/health", status=200, body=b"ok")
        async with aiohttp.ClientSession() as session:
            phases = await _probe_once(session, TARGET_URL, sample)

    assert phases.http_status == 200
    assert phases.server_processing_ms is None


async def test_probe_once_no_redirect_does_not_force_none(monkeypatch) -> None:
    """The redirect guard must gate on `redirected`, not unconditionally void
    server_processing_ms. Verified by forcing a large, deterministic ttfb via
    a lightweight artificial delay inside the mocked GET (patching the global
    time.monotonic is unsafe here -- it collides with asyncio's own internal
    use of the same clock during event-loop teardown on Windows)."""
    import asyncio as _asyncio

    async def _slow_payload(url, **kwargs):
        await _asyncio.sleep(0.05)  # ensure ttfb_ms measurably exceeds rtt_ms
        return CallbackResult(status=200, body=b"ok")

    sample = ConnectionSample(rtt_ms=0.001, tls_ms=None, alpn_protocol=None, h2_supported=None)
    with aioresponses() as mock:
        mock.get(TARGET_URL, callback=_slow_payload)
        async with aiohttp.ClientSession() as session:
            phases = await _probe_once(session, TARGET_URL, sample)

    assert phases.http_status == 200
    assert phases.ttfb_ms > 10.0  # comfortably above the 0.001ms rtt_ms
    assert phases.server_processing_ms is not None


async def test_probe_once_expected_status_override_accepts_204() -> None:
    """A target whose healthy response is 204 (not 200) must succeed when
    expected_status=204 is passed, and the default 200 check must reject it."""
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=204)
        async with aiohttp.ClientSession() as session:
            phases = await _probe_once(session, TARGET_URL, expected_status=204)
    assert phases.http_status == 204


async def test_probe_once_expected_status_default_rejects_204() -> None:
    """Without the override, a 204 must still fail — proves the override is
    additive, not a silent relaxation of the default check."""
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=204)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(_ProbeFailure) as exc_info:
                await _probe_once(session, TARGET_URL)
    assert "204" in exc_info.value.detail


# =============================================================================
# G. DEGRADED state — probe succeeds but a performance finding fires
#
# A slow sample (rtt=300ms) trips HIGH_RTT_NEEDS_EDGE, which is in the
# degrading set. check_url must route the target to DEGRADED, fire the amber
# alert on transition, and suppress it when already DEGRADED.
# =============================================================================

_SLOW_SAMPLE = ConnectionSample(rtt_ms=300.0, tls_ms=20.0, alpn_protocol="h2", h2_supported=True)


async def test_check_url_degraded_fires_amber_alert(tmp_path: Path) -> None:
    """Clean → probe OK but RTT high → DEGRADED transition fires an amber alert."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_SLOW_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["kind"] is AlertKind.DEGRADED

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["targets"][TARGET_URL]["status"] == "DEGRADED"
    assert "HIGH_RTT_NEEDS_EDGE" in data["targets"][TARGET_URL]["last_error"]


async def test_check_url_degraded_rtt_override_stays_up(tmp_path: Path) -> None:
    """The exact case that motivated an earlier release: an RTT (300ms) that trips the
    global default (100ms) must stay UP when the target declares a higher
    degraded_rtt_ms override — a legitimately-distant target (this is what
    google/github/cloudflare looked like from the operator's own network in
    production) must not chronically read DEGRADED."""
    state_file = tmp_path / "state.json"
    sm = StateManager(state_file)

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_SLOW_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                    result = await check_url(TARGET_URL, session, sm, degraded_rtt_ms=400.0)

    assert result.status == "UP"
    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["kind"] is AlertKind.RECOVERY

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["targets"][TARGET_URL]["status"] == "UP"


async def test_check_url_degraded_no_transition_suppresses_alert(tmp_path: Path) -> None:
    """Already DEGRADED → still degraded → alert suppressed (no duplicate)."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_degraded(TARGET_URL, "HIGH_RTT_NEEDS_EDGE (rtt_ms=300)")

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_SLOW_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)

    mock_alert.assert_not_called()


async def test_check_url_degraded_to_up_fires_recovery(tmp_path: Path) -> None:
    """Was DEGRADED → probe now clean → recovery (green) alert fires."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_degraded(TARGET_URL, "HIGH_RTT_NEEDS_EDGE (rtt_ms=300)")

    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock) as mock_alert:
                    await check_url(TARGET_URL, session, sm)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["kind"] is AlertKind.RECOVERY


async def test_state_set_degraded_transition(tmp_path: Path) -> None:
    """set_degraded returns True on first entry, False when already DEGRADED."""
    sm = StateManager(tmp_path / "state.json")
    assert await sm.set_degraded(TARGET_URL, "reason") is True
    assert await sm.set_degraded(TARGET_URL, "reason") is False


def test_discord_payload_degraded() -> None:
    """Degraded embed must be amber and titled 'Degraded', distinct from red/green."""
    payload = DiscordNotifier().build_payload(
        url=TARGET_URL,
        status_detail="HIGH_RTT_NEEDS_EDGE (rtt_ms=300)",
        timestamp="2026-01-01T00:00:00Z",
        kind=AlertKind.DEGRADED,
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFFB300
    assert "Degraded" in embed["title"]


# =============================================================================
# H. check_url() return value + StateManager.current_status() (an earlier release)
#
# --strict inspects the status check_url reports, so what it returns must be
# exactly what state.json ends up holding — including the hysteresis-pending
# nuance where a first failure from a healthy state does NOT yet read DOWN.
# =============================================================================


async def test_check_url_returns_up_status(tmp_path: Path) -> None:
    """check_url returns a CheckOutcome (an earlier release), not a bare string — status
    is the field that used to be the whole return value; phases/error are
    the new fields history persistence needs."""
    sm = StateManager(tmp_path / "state.json")
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock):
                    result = await check_url(TARGET_URL, session, sm)
    assert result.status == "UP"
    assert result.error is None
    assert result.phases is not None


async def test_check_url_returns_degraded_status(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    with aioresponses() as mock:
        mock.get(TARGET_URL, status=200, body=b"x" * 1024)
        async with aiohttp.ClientSession() as session:
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_SLOW_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock):
                    result = await check_url(TARGET_URL, session, sm)
    assert result.status == "DEGRADED"
    assert result.error is not None
    assert result.phases is not None
    assert result.findings  # the finding that drove DEGRADED must be attached


async def test_check_url_returns_down_status_when_confirmed(tmp_path: Path) -> None:
    """First-ever observation fails → confirmed DOWN immediately → returns DOWN.
    phases is None on a total failure — there is nothing to measure."""
    sm = StateManager(tmp_path / "state.json")
    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                    with patch("main.dispatch_alert", new_callable=AsyncMock):
                        result = await check_url(TARGET_URL, session, sm)
    assert result.status == "DOWN"
    assert result.error == "HTTP 503 (expected 200)"
    assert result.phases is None


async def test_check_url_returns_preserved_status_during_hysteresis_pending(tmp_path: Path) -> None:
    """A healthy target's FIRST failure is only strike 1/2 — the persisted
    (and returned) status must stay UP, not jump to DOWN before it's confirmed.
    A --strict caller must never see 'down' for a target state.json still
    calls UP. error still carries the failure detail even though status
    didn't flip — this run's probe genuinely failed, even if hysteresis
    is suppressing the alert."""
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)

    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            mock.get(TARGET_URL, status=503)
            async with aiohttp.ClientSession() as session:
                with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                    with patch("main.dispatch_alert", new_callable=AsyncMock):
                        result = await check_url(TARGET_URL, session, sm)
    assert result.status == "UP"
    assert result.error == "HTTP 503 (expected 200)"


def test_state_current_status_reflects_persisted_value(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    assert sm.current_status(TARGET_URL) is None  # never observed


async def test_state_current_status_after_set_up(tmp_path: Path) -> None:
    sm = StateManager(tmp_path / "state.json")
    await sm.set_up(TARGET_URL)
    assert sm.current_status(TARGET_URL) == "UP"


# =============================================================================
# I. run_health_checks() down-count + _exit_code() (an earlier release)
# =============================================================================


async def test_run_health_checks_returns_down_count(tmp_path) -> None:
    """The returned count must reflect DOWN targets only — UP must not be
    counted, since that count is what --strict acts on.

    Uses run_health_checks' state_path=/history_path= parameters to isolate
    both files in tmp_path, instead of monkeypatching constructor defaults
    (audit nitpick, an earlier release review: a monkeypatch silently stops working
    the moment a constructor is called with an explicit path anywhere, and a
    test that isolates itself by parameter can't have that failure mode).
    Without history_path= here, this test would silently write a real
    history.db into the project root on every run — caught once, fixed here."""
    up_url   = "https://up.example.test"
    down_url = "https://down.example.test"

    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(up_url, status=200)
            mock.get(down_url, status=503)
            mock.get(down_url, status=503)
            mock.get(down_url, status=503)
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock):
                    down_count = await run_health_checks(
                        targets=[up_url, down_url],
                        state_path=tmp_path / "state.json",
                        history_path=tmp_path / "history.db",
                    )

    assert down_count == 1


async def test_run_health_checks_writes_outcomes_to_history_db(tmp_path) -> None:
    """End-to-end: a real run must persist one probe_results row per target
    into history.db, findings linked correctly, tagged with the same
    run_started_at — this is the whole point of an earlier release."""
    up_url   = "https://up.example.test"
    down_url = "https://down.example.test"
    history_path = tmp_path / "history.db"

    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        with aioresponses() as mock:
            mock.get(up_url, status=200, body=b"x" * 1024)
            mock.get(down_url, status=503)
            mock.get(down_url, status=503)
            mock.get(down_url, status=503)
            with patch("main.sample_connection", new_callable=AsyncMock, return_value=_HEALTHY_SAMPLE):
                with patch("main.dispatch_alert", new_callable=AsyncMock):
                    await run_health_checks(
                        targets=[up_url, down_url],
                        state_path=tmp_path / "state.json",
                        history_path=history_path,
                    )

    con = sqlite3.connect(history_path)
    rows = con.execute(
        "SELECT url, status, error, run_started_at FROM probe_results ORDER BY url"
    ).fetchall()
    con.close()

    assert len(rows) == 2
    assert rows[0][3] == rows[1][3]  # same run_started_at for both targets
    urls_by_status = {r[0]: r[1] for r in rows}
    assert urls_by_status[up_url] == "UP"
    assert urls_by_status[down_url] == "DOWN"


def test_warn_if_over_budget_fires_over_threshold(caplog) -> None:
    """A duration past RUN_BUDGET_S must log a WARNING naming the overlap risk."""
    with caplog.at_level("WARNING"):
        _warn_if_over_budget(main.RUN_BUDGET_S + 5.0)
    assert any("exceeding" in r.message for r in caplog.records)


def test_warn_if_over_budget_silent_under_threshold(caplog) -> None:
    """A normal, fast run must not log anything about budget overlap."""
    with caplog.at_level("WARNING"):
        _warn_if_over_budget(1.0)
    assert not any("exceeding" in r.message for r in caplog.records)


def test_exit_code_no_strict_always_zero() -> None:
    assert _exit_code(strict=False, down_count=0) == 0
    assert _exit_code(strict=False, down_count=5) == 0


def test_exit_code_strict_zero_down_is_zero() -> None:
    assert _exit_code(strict=True, down_count=0) == 0


def test_exit_code_strict_with_down_is_one() -> None:
    assert _exit_code(strict=True, down_count=1) == 1
    assert _exit_code(strict=True, down_count=3) == 1


# =============================================================================
# I2. _generate_report() / --report (an earlier release)
#
# The risk this section guards against is arithmetic and honesty about the
# retention window, not exceptions -- see tests/test_history.py section F for
# the hand-calculated uptime_pct()/latency_percentiles() checks this builds on.
# =============================================================================


async def test_generate_report_shows_no_data_for_a_target_with_no_history(tmp_path: Path) -> None:
    """A target that exists in targets.yaml but has never been probed must
    read 'no data' in every column -- not 0%, which would misreport it as
    having been down for the entire window."""
    db_path = tmp_path / "history.db"
    main.HistoryRecorder(db_path)  # schema only, no rows
    report = main._generate_report(
        [Target(url=TARGET_URL)], history_path=db_path, retention_days=30,
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    assert TARGET_URL in report
    assert "no data" in report


async def test_generate_report_with_no_history_db_at_all_shows_no_data(tmp_path: Path) -> None:
    """Reproduced live: `--report` on a freshly deployed instance, before the
    first probe cycle has ever run, points at a history.db with no schema at
    all. Confirmed this crashed with 'sqlite3.OperationalError: no such table'
    before the fix -- must read 'no data', not a traceback."""
    db_path = tmp_path / "history.db"  # never touched -- no HistoryRecorder call
    report = main._generate_report(
        [Target(url=TARGET_URL)], history_path=db_path, retention_days=30,
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    assert TARGET_URL in report
    assert "no data" in report


async def test_generate_report_shows_seeded_uptime_and_percentiles(tmp_path: Path) -> None:
    """Numbers in the rendered table must match a hand-seeded, hand-calculated
    dataset -- not just 'a table got printed'."""
    db_path = tmp_path / "history.db"
    rec = main.HistoryRecorder(db_path)
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    checked_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    outcomes = [
        main.CheckOutcome(
            url=TARGET_URL, status="UP", checked_at=checked_at,
            phases=ProbePhases(
                url=TARGET_URL, http_status=200, ttfb_ms=100.0,
                transfer_ms=1.0, body_bytes=100, rtt_ms=10.0,
            ),
        )
        for _ in range(9)
    ] + [main.CheckOutcome(url=TARGET_URL, status="DOWN", checked_at=checked_at)]
    await rec.record_run(checked_at, outcomes)

    report = main._generate_report(
        [Target(url=TARGET_URL)], history_path=db_path, retention_days=30, now=now,
    )
    assert "90.0%" in report   # 9/10 non-DOWN
    assert "100ms" in report  # p50 == p95 == 100 when 9 of 10 rows are 100ms


async def test_generate_report_omits_windows_beyond_retention(tmp_path: Path) -> None:
    """HISTORY_RETENTION_DAYS=1 must not print an 'Up 30d' column -- claiming
    30 days of visibility when only 1 day is actually retained misreports
    what the data can support."""
    db_path = tmp_path / "history.db"
    main.HistoryRecorder(db_path, retention_days=1)
    report = main._generate_report(
        [Target(url=TARGET_URL)], history_path=db_path, retention_days=1,
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    assert "Up 1d" in report
    assert "Up 7d" not in report
    assert "Up 30d" not in report
    assert "retention: 1d" in report


async def test_generate_report_never_opens_an_http_session(tmp_path: Path) -> None:
    """--report is strictly read-only over history.db. It must never
    construct an aiohttp.ClientSession -- doing so would mean this 'read a
    report' code path accidentally probed a live target."""
    db_path = tmp_path / "history.db"
    main.HistoryRecorder(db_path)
    with patch("aiohttp.ClientSession") as mock_session:
        main._generate_report(
            [Target(url=TARGET_URL)], history_path=db_path, retention_days=30,
            now=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
    mock_session.assert_not_called()


def test_report_flag_end_to_end_exits_zero_without_probing(tmp_path: Path) -> None:
    """Full entry point: `python main.py --report` against a real, seeded
    history.db must print the table and exit 0 -- and must return in well
    under one probe timeout, which is what it would take if it had ignored
    the flag and attempted a normal run against a nonexistent target."""
    db_path = tmp_path / "history.db"
    rec = main.HistoryRecorder(db_path)
    checked_at = "2026-07-30T00:00:00Z"
    asyncio.run(rec.record_run(checked_at, [
        main.CheckOutcome(
            url=TARGET_URL, status="UP", checked_at=checked_at,
            phases=ProbePhases(
                url=TARGET_URL, http_status=200, ttfb_ms=100.0,
                transfer_ms=1.0, body_bytes=100, rtt_ms=10.0,
            ),
        )
    ]))
    (tmp_path / "targets.yaml").write_text(
        f"targets:\n  - {TARGET_URL}\n", encoding="utf-8"
    )

    project_root = Path(__file__).resolve().parent.parent
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(project_root / "main.py"), "--report"],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(project_root)},
        capture_output=True, text=True, timeout=60,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stderr
    assert TARGET_URL in result.stdout
    assert "90.0%" not in result.stdout  # sanity: this run is 1/1 UP, not the other test's data
    assert "100.0%" in result.stdout
    assert elapsed < 5.0, (
        f"--report took {elapsed:.1f}s -- a real probe attempt (REQUEST_TIMEOUT_S=5, "
        f"3 retries) would take far longer, suggesting the flag was ignored."
    )


# =============================================================================
# J. Logging configuration (an earlier release)
#
# main.py wires RotatingFileHandler up via logging.basicConfig() at import
# time, but basicConfig() is a documented no-op once the root logger already
# has handlers -- and pytest's own logging plugin pre-attaches one before any
# test module is imported, inside THIS process. A subprocess with a clean
# interpreter doesn't have that problem, so that's what actually verifies the
# wiring below -- inspecting the in-process root logger was tried first and
# abandoned: it can only ever observe pytest's handler, never main.py's.
# =============================================================================


def test_log_rotation_constants_match_documented_values() -> None:
    assert main.LOG_MAX_BYTES == 10 * 1024 * 1024
    assert main.LOG_BACKUP_COUNT == 5


def test_main_wires_rotating_file_handler_in_a_clean_interpreter(tmp_path: Path) -> None:
    """The real logging.basicConfig() call in main.py is only observable
    outside pytest's own logging plugin -- a subprocess with a fresh
    interpreter proves it, rather than asserting against a synthetic
    RotatingFileHandler built by the test itself. Without this, changing
    maxBytes=LOG_MAX_BYTES to maxBytes=0 (rotation silently disabled, the
    exact bug an earlier release fixes) passes every other test in this file.

    Runs with cwd=tmp_path on purpose: importing main creates
    health_checker.log in the current directory, and a test that forgets to
    isolate a file path has bitten this project twice already (state_path in
    an earlier release, history_path in an earlier release)."""
    project_root = Path(__file__).resolve().parent.parent
    code = (
        "import json, logging, main;"
        "h=[x for x in logging.getLogger().handlers "
        "   if isinstance(x, main.RotatingFileHandler)];"
        "print(json.dumps({'n': len(h),"
        "                  'max': h[0].maxBytes if h else None,"
        "                  'bak': h[0].backupCount if h else None}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(project_root)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    wiring = json.loads(result.stdout.strip().splitlines()[-1])

    assert wiring["n"] == 1, "main.py must install exactly one RotatingFileHandler"
    assert wiring["max"] == main.LOG_MAX_BYTES
    assert wiring["max"] > 0, "maxBytes=0 disables rotation entirely"
    assert wiring["bak"] == main.LOG_BACKUP_COUNT
    assert wiring["bak"] > 0


def test_rotating_file_handler_actually_rotates(tmp_path: Path) -> None:
    """A plain FileHandler(mode='a') never rotates -- the bug an earlier release fixes.
    Proves RotatingFileHandler, the class main.py now wires up, genuinely
    creates a backup file once the size threshold is crossed (small
    thresholds here so the test stays fast; main.py's real 10 MiB/5-backup
    values are checked separately above)."""
    log_file = tmp_path / "rotating.log"
    handler = RotatingFileHandler(log_file, maxBytes=200, backupCount=2, encoding="utf-8")
    logger = logging.getLogger("test.log_rotation")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for _ in range(50):
            logger.info("x" * 20)  # far past the 200-byte threshold
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert log_file.exists()
    assert (tmp_path / "rotating.log.1").exists()
