"""
tests/test_api.py — Test suite for api.py (read-only JSON endpoint).

Coverage:
  A. read_state()        — both on-disk shapes, missing file, garbage
  B. stale_seconds()     — the metric that separates alive from showing-the-past
  C. history_payload()   — aggregates, and agreement with --report
  D. HTTP surface        — routes, status codes, error bodies
  E. Security posture    — loopback default, read-only enforcement, no leakage

Reviewer notes
--------------
The test that matters most is not "does the endpoint answer" — it is
test_api_and_report_agree_exactly. A CLI and an API deriving the same figure
from the same rows and printing different numbers is a credibility bug that
no amount of individually-passing tests would catch, because each side would
be self-consistent. That is why it compares one against the other rather than
each against a fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import main
from api import (
    DEFAULT_HOST,
    WINDOWS,
    history_payload,
    read_state,
    serve,
    stale_seconds,
)
from diagnostics import ProbePhases
from history import CheckOutcome, HistoryRecorder

URL = "https://example.com"
# Relative to now, not a hand-written date.
#
# This was datetime(2026, 7, 30): correct the day it was written and false a
# month later. _seed_history promises rows "inside every window", and the
# windows are measured against the real clock, so a fixed date ages out of the
# window on its own and turns three tests red with no change to the code. A
# test whose correctness depends on the day it runs does not verify the code:
# it verifies the calendar.
# microsecond=0 is not cosmetic: _stamp() formats to whole seconds, so a NOW
# carrying microseconds is rounded on the way out and comes back different on
# the way in. stale_seconds subtractions stop landing on exactly 3600.0 and
# start returning 3600.000123 -- an equality test that fails on the clock
# rather than on the code.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_history(db_path: Path, ttfbs: list[float], statuses: list[str]) -> None:
    """Write one row per (ttfb, status) pair, all inside every window."""
    rec = HistoryRecorder(db_path)
    outcomes = [
        CheckOutcome(
            url=URL, status=status, checked_at=_stamp(NOW - timedelta(minutes=1)),
            phases=(
                ProbePhases(url=URL, http_status=200, ttfb_ms=ttfb,
                            transfer_ms=1.0, body_bytes=100, rtt_ms=10.0)
                if status != "DOWN" else None
            ),
        )
        for ttfb, status in zip(ttfbs, statuses)
    ]
    asyncio.run(rec.record_run(_stamp(NOW), outcomes))


# =============================================================================
# A. read_state()
# =============================================================================


def test_read_state_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    """A freshly deployed instance has no state file yet. That is new, not
    broken -- answering with nothing is honest; a 500 would not be."""
    assert read_state(tmp_path / "state.json") == {}


def test_read_state_reads_the_wrapped_schema(tmp_path: Path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({
        "schema_version": 4,
        "targets": {URL: {"status": "UP", "last_checked": _stamp(NOW)}},
    }), encoding="utf-8")
    assert read_state(f)[URL]["status"] == "UP"


def test_read_state_reads_the_legacy_flat_schema(tmp_path: Path) -> None:
    """A state.json written before schema versioning WAS the flat mapping, with
    no wrapper. The API
    must not go blind against a file StateManager itself still understands."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps({URL: {"status": "DOWN"}}), encoding="utf-8")
    assert read_state(f)[URL]["status"] == "DOWN"


def test_read_state_survives_corrupt_json(tmp_path: Path) -> None:
    f = tmp_path / "state.json"
    f.write_text("{not json at all", encoding="utf-8")
    assert read_state(f) == {}


def test_read_state_agrees_with_statemanager_on_a_malformed_file(tmp_path: Path) -> None:
    """A hand-edited state.json with
    schema_version but no targets key made this function fall back to the
    whole object, serving schema_version itself as if it were a monitored
    URL — a dashboard iterating the map would then read a status off an
    integer. StateManager falls back to {} on the same input, and this
    function's own docstring claims to mirror it; now it does."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"schema_version": 4, "notes": "stray key"}), encoding="utf-8")
    assert read_state(f) == {}
    assert read_state(f) == main.StateManager(f)._state


UNREADABLE_STATES = [
    pytest.param("{broken", id="invalid-json"),
    pytest.param("[]", id="json-array"),
    pytest.param('{"schema_version": 2, "targets": []}', id="targets-not-an-object"),
]


@pytest.mark.parametrize("content", UNREADABLE_STATES)
def test_read_state_checked_names_the_problem_with_an_unusable_file(
        tmp_path: Path, content) -> None:
    """An empty map alone reads as "nothing is monitored". A file that exists
    and cannot be used must come back with the reason, or the dashboard, the
    API and the MCP server all show a healthy-looking nothing."""
    f = tmp_path / "state.json"
    f.write_text(content, encoding="utf-8")
    targets, problem = api.read_state_checked(f)
    assert targets == {}
    assert isinstance(problem, str) and problem
    assert api.read_state(f) == {}


def test_read_state_checked_has_no_problem_for_a_missing_file(tmp_path: Path) -> None:
    """A fresh deployment has no state file; that is not a fault to report."""
    assert api.read_state_checked(tmp_path / "state.json") == ({}, None)


def test_read_state_checked_has_no_problem_for_a_valid_file(tmp_path: Path) -> None:
    """Control: the problem string appears only for the broken shapes above."""
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"schema_version": 4, "targets": {URL: {"status": "UP"}}}),
                 encoding="utf-8")
    assert api.read_state_checked(f) == ({URL: {"status": "UP"}}, None)


# =============================================================================
# B. stale_seconds()
# =============================================================================


def test_stale_seconds_reports_the_oldest_target(tmp_path: Path) -> None:
    """The oldest, not the newest: one target going unprobed is exactly the
    condition worth surfacing, and an average would hide it."""
    targets = {
        "a": {"last_checked": _stamp(NOW - timedelta(seconds=30))},
        "b": {"last_checked": _stamp(NOW - timedelta(seconds=3600))},
    }
    assert stale_seconds(targets, now=NOW) == 3600.0


def test_stale_seconds_is_none_with_nothing_to_age() -> None:
    assert stale_seconds({}, now=NOW) is None
    assert stale_seconds({"a": {"status": "UP"}}, now=NOW) is None


def test_stale_seconds_ignores_unparseable_timestamps() -> None:
    targets = {
        "a": {"last_checked": "not a timestamp"},
        "b": {"last_checked": _stamp(NOW - timedelta(seconds=60))},
    }
    assert stale_seconds(targets, now=NOW) == 60.0


# =============================================================================
# C. history_payload() — and the agreement that matters
# =============================================================================


def test_history_payload_reports_seeded_numbers(tmp_path: Path) -> None:
    """The dataset deliberately mixes DEGRADED in: with 9 UP + 1 DOWN the
    answer is 90% whether DEGRADED counts as up or not, so such a seed would
    pass against either rule and prove nothing. 7 UP + 2 DEGRADED + 1 DOWN
    reads 90% only under this project's rule (DEGRADED is up and hurting,
    which is an availability success), and 70% under the other."""
    db = tmp_path / "history.db"
    _seed_history(
        db,
        [100.0] * 9 + [0.0],
        ["UP"] * 7 + ["DEGRADED"] * 2 + ["DOWN"],
    )
    payload = history_payload(db, "7d", [URL])
    assert payload["window"] == "7d"
    assert payload["targets"][URL]["uptime_pct"] == 90.0
    assert payload["targets"][URL]["p50_ttfb_ms"] == 100.0


def test_history_payload_no_data_is_null_not_zero(tmp_path: Path) -> None:
    """Zero percent claims the target was down all window. A target nobody
    has probed has made no such claim -- null says so, 0.0 lies."""
    db = tmp_path / "history.db"
    HistoryRecorder(db)
    payload = history_payload(db, "7d", [URL])
    assert payload["targets"][URL]["uptime_pct"] is None
    assert payload["targets"][URL]["p50_ttfb_ms"] is None


def test_api_and_report_agree_exactly(tmp_path: Path) -> None:
    """THE test here. A CLI and an API deriving the same figure from
    the same rows and printing different numbers is a credibility bug that no
    individually-passing test would catch, because each side is self-consistent.
    So this compares one against the other."""
    db = tmp_path / "history.db"
    _seed_history(db, [50.0, 100.0, 150.0, 200.0], ["UP", "UP", "DEGRADED", "DOWN"])

    payload = history_payload(db, "30d", [URL])["targets"][URL]
    report  = main._generate_report(
        [main.Target(url=URL)], history_path=db, retention_days=30, now=NOW,
    )

    # The report renders uptime as "75.0%"; the API as the float 75.0.
    assert f"{payload['uptime_pct']:.1f}%" in report
    assert f"{payload['p50_ttfb_ms']:.0f}ms" in report
    assert f"{payload['p95_ttfb_ms']:.0f}ms" in report


# =============================================================================
# D. HTTP surface
# =============================================================================


@pytest.fixture
def live_server(tmp_path: Path):
    """A real server on an ephemeral port, torn down after the test."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "schema_version": 4,
        "targets": {URL: {"status": "UP", "last_checked": _stamp(
            datetime.now(timezone.utc)
        )}},
    }), encoding="utf-8")
    db = tmp_path / "history.db"
    _seed_history(db, [100.0, 200.0], ["UP", "UP"])

    server = serve(DEFAULT_HOST, 0, state_path=state, history_path=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{DEFAULT_HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_status_endpoint_serves_state(live_server) -> None:
    status, body = _get(live_server, "/api/status")
    assert status == 200
    assert body["targets"][URL]["status"] == "UP"
    assert body["stale_seconds"] is not None


def test_status_endpoint_logs_the_staleness_metric(live_server, caplog) -> None:
    """api_stale_state_seconds is the metric that matters here, and the code
    once returned it in the body but never logged it. A number only visible to whoever
    happens to look is no defence against 'nobody is looking', which is
    exactly the condition it exists to detect — a dashboard serving
    yesterday's data with a 200."""
    with caplog.at_level("INFO"):
        status, body = _get(live_server, "/api/status")
    assert status == 200
    assert any(
        "metric=api_stale_state_seconds" in r.message
        and f"value={body['stale_seconds']:.1f}" in r.message
        for r in caplog.records
    )


def test_history_endpoint_serves_aggregates(live_server) -> None:
    status, body = _get(live_server, "/api/history")
    assert status == 200
    assert body["window"] == "7d"
    assert body["targets"][URL]["uptime_pct"] == 100.0


@pytest.mark.parametrize("window", sorted(WINDOWS))
def test_history_endpoint_accepts_every_documented_window(live_server, window) -> None:
    status, body = _get(live_server, f"/api/history?window={window}")
    assert status == 200
    assert body["window"] == window


def test_unknown_window_is_a_400_naming_the_accepted_values(live_server) -> None:
    """A rejection that does not say what WOULD work makes the caller guess."""
    status, body = _get(live_server, "/api/history?window=99y")
    assert status == 400
    assert "accepted" in body


def test_unknown_route_is_a_json_404_not_a_stack_trace(live_server) -> None:
    status, body = _get(live_server, "/admin")
    assert status == 404
    assert body["error"] == "not found"


def _fetch(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


@pytest.fixture
def server_with_state(tmp_path: Path, monkeypatch):
    """Factory: a live server whose state.json holds exactly the text given
    (None for no file at all). The target store is pointed at nothing so the
    host's data/ cannot change what the page shows."""
    started = []

    def start(state_text: str | None):
        import targetstore
        monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "no-store" / "targets.json")
        monkeypatch.setattr(targetstore, "SEED_FILE", tmp_path / "no-seed.yaml")
        state = tmp_path / "state.json"
        if state_text is not None:
            state.write_text(state_text, encoding="utf-8")
        db = tmp_path / "history.db"
        _seed_history(db, [100.0], ["UP"])
        srv = serve(DEFAULT_HOST, 0, state_path=state, history_path=db)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return f"http://{DEFAULT_HOST}:{srv.server_address[1]}"

    yield start
    for srv, thread in started:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_status_endpoint_has_a_null_state_error_for_a_valid_file(live_server) -> None:
    status, body = _get(live_server, "/api/status")
    assert status == 200
    assert body["state_error"] is None


@pytest.mark.parametrize("content", UNREADABLE_STATES)
def test_status_endpoint_says_why_the_targets_are_empty(server_with_state, content) -> None:
    """200 with targets {} and nothing else is what a monitor with no targets
    answers. state_error is what tells an alerting pipeline the file is the
    problem."""
    base = server_with_state(content)
    status, body = _get(base, "/api/status")
    assert status == 200
    assert body["targets"] == {}
    assert isinstance(body["state_error"], str) and body["state_error"]


def test_status_endpoint_has_a_null_state_error_when_there_is_no_file(server_with_state) -> None:
    status, body = _get(server_with_state(None), "/api/status")
    assert status == 200
    assert body == {"targets": {}, "stale_seconds": None, "state_error": None, "warnings": []}


@pytest.mark.parametrize("path", ["/", "/partial/targets"])
def test_the_dashboard_says_the_state_cannot_be_read(server_with_state, path) -> None:
    """The page and the fragment it polls both carry the notice: one only on
    the first paint would be erased by the first poll."""
    base = server_with_state("{broken")
    status, markup = _fetch(base, path)
    assert status == 200
    assert "cannot be read" in markup


@pytest.mark.parametrize("path", ["/", "/partial/targets"])
def test_the_dashboard_has_no_unreadable_notice_for_a_missing_file(
        server_with_state, path) -> None:
    """Control: a fresh deployment has no state file and is not a fault."""
    assert "cannot be read" not in _fetch(server_with_state(None), path)[1]


def test_the_dashboard_has_no_unreadable_notice_for_a_valid_file(live_server) -> None:
    for path in ("/", "/partial/targets"):
        assert "cannot be read" not in _fetch(live_server, path)[1]


# =============================================================================
# E. Security posture
# =============================================================================


def test_default_bind_is_loopback() -> None:
    """Publishing which targets exist, when they fail and with what error is
    a decision. Off-loopback must take a deliberate act, never a default."""
    assert api.DEFAULT_HOST == "127.0.0.1"


def test_report_queries_open_the_database_read_only(tmp_path: Path) -> None:
    """Driver-level, not intent-level: this process runs beside a live writer,
    so a write must be impossible rather than merely unintended."""
    db = tmp_path / "history.db"
    _seed_history(db, [100.0], ["UP"])
    from history import _connect_read_only

    con = _connect_read_only(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute(
                "INSERT INTO probe_results (run_started_at, url, checked_at, status) "
                "VALUES ('x','y','z','UP')"
            )
    finally:
        con.close()


def test_responses_carry_no_secrets(live_server, tmp_path: Path) -> None:
    """last_error already arrives redacted from an earlier release, but the API is a new
    sink and must be checked as one rather than assumed safe."""
    for path in ("/api/status", "/api/history"):
        _, body = _get(live_server, path)
        rendered = json.dumps(body).lower()
        for secret_marker in ("discord.com/api/webhooks", "hooks.slack.com", "webhook"):
            assert secret_marker not in rendered
