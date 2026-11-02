"""
tests/test_mcp_server.py -- the read-only MCP server, v0.

An MCP client (an assistant, an IDE) asks the monitor what it knows: current
status, uptime and percentiles per window, and the p95 trend. Three properties
are the contract, and each one is a way this has gone wrong elsewhere:

  - Same numbers as the API and --report. The tools call the functions the
    dashboard API and the report already use; a second query path is how two
    surfaces end up answering differently for the same rows. The tests compare
    against those surfaces, not against the server itself.
  - Read-only by construction. The history database is opened with mode=ro and
    no tool writes state; a test hashes both files around every call, and a
    missing database stays missing instead of being created empty.
  - stdout is the protocol. MCP over stdio is one JSON-RPC message per line on
    stdout. A single log line there corrupts the stream for the client, so the
    server is run as a real subprocess and every stdout line must parse.

Coverage:
  A. Handshake: initialize, version negotiation, initialized, ping, tools/list
  B. Tools agree with the API and --report
  C. Errors: JSON-RPC codes, tool errors that do not crash the loop
  D. Read-only
  E. A real subprocess: stdout carries only protocol
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import main
import mcp_server
from diagnostics import ProbePhases
from history import CheckOutcome, HistoryRecorder

ROOT = Path(__file__).resolve().parent.parent
URL  = "https://example.test/"
NOW  = datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(db: Path) -> None:
    """25 probes 8 days ago at 100 ms and 25 a day ago at 400 ms: enough in
    both trend windows for a verdict, and a change large enough to be one."""
    rec = HistoryRecorder(db)

    def outcome(when: datetime, ttfb: float) -> CheckOutcome:
        return CheckOutcome(
            url=URL, status="UP", checked_at=_stamp(when),
            phases=ProbePhases(url=URL, http_status=200, ttfb_ms=ttfb,
                               transfer_ms=1.0, body_bytes=100, rtt_ms=10.0),
        )

    for i in range(25):
        old = NOW - timedelta(days=8, minutes=i)
        new = NOW - timedelta(days=1, minutes=i)
        asyncio.run(rec.record_run(_stamp(old), [outcome(old, 100.0)]))
        asyncio.run(rec.record_run(_stamp(new), [outcome(new, 400.0)]))


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state.json"
    # indent=2: not what json.dumps writes by default, so a tool that re-saves
    # the same data still changes the bytes.
    state.write_text(json.dumps({
        "schema_version": 2,
        "targets": {URL: {"status": "UP", "last_checked": _stamp(NOW)}},
    }, indent=2), encoding="utf-8")
    db = tmp_path / "history.db"
    _seed(db)
    return state, db


@pytest.fixture
def server(paths) -> mcp_server.Server:
    # A .env that does not exist: the default is the repository's, and a test
    # must not depend on what the owner's real .env holds.
    return mcp_server.Server(*paths, env_file=paths[0].parent / ".env")


def _req(method: str, params: dict | None = None, id_: int = 1) -> str:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _call(server, name: str, arguments: dict | None = None) -> dict:
    out = server.handle_line(_req("tools/call", {"name": name, "arguments": arguments or {}}))
    return json.loads(out)


# =============================================================================
# A. Handshake
# =============================================================================


def test_the_version_is_0_2_0() -> None:
    """0.x until the owner calls it finished; 1.0.0 then. 0.2.0 adds the
    alerting state to get_status."""
    assert mcp_server.SERVER_VERSION == "0.2.0"


def test_initialize_echoes_a_supported_protocol_version(server) -> None:
    out = json.loads(server.handle_line(_req("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}})))
    result = out["result"]
    assert out["id"] == 1 and out["jsonrpc"] == "2.0"
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"] == {"name": "vigil-sre", "version": mcp_server.SERVER_VERSION}
    assert "tools" in result["capabilities"]


def test_initialize_answers_its_own_latest_version_for_an_unknown_one(server) -> None:
    out = json.loads(server.handle_line(_req("initialize", {
        "protocolVersion": "1999-01-01", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}})))
    assert out["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]


def test_a_notification_gets_no_answer(server) -> None:
    assert server.handle_line(json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


def test_ping_answers_an_empty_result(server) -> None:
    assert json.loads(server.handle_line(_req("ping")))["result"] == {}


def test_tools_list_names_the_three_tools_with_schemas(server) -> None:
    tools = json.loads(server.handle_line(_req("tools/list")))["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_status", "get_history", "get_trend"}
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


# =============================================================================
# B. The tools agree with the API and --report
# =============================================================================


def test_get_status_is_what_api_status_serves(server, paths) -> None:
    state, _ = paths
    result = _call(server, "get_status")["result"]
    targets = api.read_state(state)
    content = result["structuredContent"]
    assert result["isError"] is False
    assert content["targets"] == targets
    assert content["stale_seconds"] == pytest.approx(api.stale_seconds(targets), abs=5)
    assert json.loads(result["content"][0]["text"]) == content


@pytest.mark.parametrize("window", ["1d", "7d", "30d"])
def test_get_history_is_what_api_history_serves(server, paths, window) -> None:
    state, db = paths
    result = _call(server, "get_history", {"window": window})["result"]
    expected = api.history_payload(db, window, list(api.read_state(state)))
    assert result["isError"] is False
    assert result["structuredContent"] == expected
    assert json.loads(result["content"][0]["text"]) == expected


def test_get_history_defaults_to_the_api_default_window(server) -> None:
    result = _call(server, "get_history")["result"]
    assert result["structuredContent"]["window"] == api.DEFAULT_WINDOW


def test_get_trend_gives_the_verdict_report_prints(server, paths) -> None:
    _, db = paths
    result = _call(server, "get_trend")["result"]
    verdict = result["structuredContent"]["targets"][URL]
    assert verdict["verdict"] == "degrading"
    assert verdict["previous_p95_ms"] == pytest.approx(100.0)
    assert verdict["recent_p95_ms"] == pytest.approx(400.0)
    report = main._trend_lines([main.Target(url=URL)], db)
    assert any(URL in line and "DEGRADING" in line for line in report), report


def test_the_trend_window_is_the_reports(server) -> None:
    assert mcp_server.TREND_WINDOW_DAYS == main.TREND_WINDOW_DAYS
    result = _call(server, "get_trend")["result"]
    assert result["structuredContent"]["window_days"] == main.TREND_WINDOW_DAYS


# =============================================================================
# B2. get_status says whether anyone would hear about an outage (0.2.0)
#
# The monitor warns every round when no alert channel is configured and when
# HEARTBEAT_URL is unset. An MCP that reported every target UP and said nothing
# of that answered "is it healthy?" with half the truth.
# =============================================================================

SECRET_HOOK = "https://hooks.example.test/T000/very-secret-webhook-path"
SECRET_PING = "https://hc-ping.example.test/very-secret-ping-uuid"


def _status(server) -> dict:
    return _call(server, "get_status")["result"]


def _clear_channels(monkeypatch) -> None:
    for var in ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "HEARTBEAT_URL"):
        monkeypatch.delenv(var, raising=False)


def test_no_channel_and_no_heartbeat_are_reported(server, monkeypatch) -> None:
    _clear_channels(monkeypatch)
    content = _status(server)["structuredContent"]
    assert content["alerting"] == {"channels": [], "heartbeat": False}
    codes = [w["code"] for w in content["warnings"]]
    assert codes == ["no_alert_channels", "heartbeat_unconfigured"]


def test_configured_channels_are_named_and_silence_their_warning(server, monkeypatch) -> None:
    _clear_channels(monkeypatch)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET_HOOK)
    monkeypatch.setenv("HEARTBEAT_URL", SECRET_PING)
    content = _status(server)["structuredContent"]
    assert content["alerting"] == {"channels": ["Discord"], "heartbeat": True}
    assert content["warnings"] == []


def test_a_secret_never_appears_in_the_answer(server, monkeypatch) -> None:
    """Names and booleans only. The full protocol line is searched, not just
    the structured part: a URL in the text content would leak just the same."""
    _clear_channels(monkeypatch)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET_HOOK)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", SECRET_HOOK + "-slack")
    monkeypatch.setenv("HEARTBEAT_URL", SECRET_PING)
    line = server.handle_line(_req("tools/call", {"name": "get_status", "arguments": {}}))
    for fragment in ("very-secret", "hooks.example", "hc-ping"):
        assert fragment not in line


def test_the_env_file_counts_like_the_monitor_counts_it(paths, monkeypatch) -> None:
    """Run from the repository, the monitor loads .env; an MCP that ignored it
    would report "no channels" for a monitor that has one -- a false alarm,
    and the opposite failure is no better. A variable already in the process
    environment wins, as load_dotenv(override=False) does."""
    _clear_channels(monkeypatch)
    env_file = paths[0].parent / ".env"
    env_file.write_text(f"SLACK_WEBHOOK_URL={SECRET_HOOK}\nHEARTBEAT_URL=\n", encoding="utf-8")
    server = mcp_server.Server(*paths, env_file=env_file)
    content = _status(server)["structuredContent"]
    assert content["alerting"] == {"channels": ["Slack"], "heartbeat": False}
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    assert _status(server)["structuredContent"]["alerting"]["channels"] == []


async def test_the_warnings_are_the_monitors_own(paths, monkeypatch, caplog) -> None:
    """Each warning the MCP reports opens with the same words the monitor logs
    for the same condition in a real round: one condition, one wording."""
    import logging
    from unittest.mock import AsyncMock
    _clear_channels(monkeypatch)
    monkeypatch.setattr(main, "RETRY_ATTEMPTS", 1)

    async def _up(*a, **k):
        return main.CheckOutcome(url=a[0], status=main.STATUS_UP,
                                 checked_at=_stamp(NOW), phases=None)
    monkeypatch.setattr(main, "check_url", _up)
    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        await main.run_health_checks(targets=[main.Target(url=URL)],
                                     state_path=paths[0].parent / "s2.json",
                                     history_path=paths[0].parent / "h2.db")
    logged = [r.getMessage() for r in caplog.records]
    server = mcp_server.Server(*paths, env_file=paths[0].parent / ".env")
    warnings = _status(server)["structuredContent"]["warnings"]
    assert warnings
    for w in warnings:
        head = w["message"].split(" — ")[0]
        assert any(m.startswith(head) for m in logged), (head, logged)


# =============================================================================
# C. Errors
# =============================================================================


def test_malformed_json_is_a_parse_error_with_a_null_id(server) -> None:
    out = json.loads(server.handle_line("{not json"))
    assert out["error"]["code"] == -32700 and out["id"] is None


@pytest.mark.parametrize("line", ['[1, 2]', '"text"', '{"jsonrpc": "2.0", "id": 3}'])
def test_something_that_is_not_a_request_is_invalid(server, line) -> None:
    out = json.loads(server.handle_line(line))
    assert out["error"]["code"] == -32600


def test_an_unknown_method_is_method_not_found(server) -> None:
    out = json.loads(server.handle_line(_req("resources/list", id_=7)))
    assert out["error"]["code"] == -32601 and out["id"] == 7


def test_an_unknown_tool_is_invalid_params(server) -> None:
    out = _call(server, "delete_everything")
    assert out["error"]["code"] == -32602


def test_a_bad_window_is_a_tool_error_that_names_the_accepted_ones(server) -> None:
    result = _call(server, "get_history", {"window": "1y"})["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "1y" in text and all(w in text for w in api.WINDOWS)


def test_a_missing_database_is_a_tool_error_and_is_not_created(paths) -> None:
    state, db = paths
    missing = db.with_name("absent.db")
    server = mcp_server.Server(state, missing)
    for name in ("get_history", "get_trend"):
        result = _call(server, name)["result"]
        assert result["isError"] is True, name
    assert not missing.exists()


def test_the_loop_survives_every_error(paths) -> None:
    """One bad message must not end the session: the client would see the
    server die and every later question fail."""
    import io
    lines = ["{bad", _req("nope", id_=2), _req("tools/call", {"name": "x"}, id_=3),
             _req("ping", id_=4)]
    out = io.StringIO()
    mcp_server.run(io.StringIO("\n".join(lines) + "\n"), out, mcp_server.Server(*paths))
    answers = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == [None, 2, 3, 4]
    assert answers[-1]["result"] == {}


# =============================================================================
# D. Read-only
# =============================================================================


def _fingerprint(directory: Path) -> dict[str, tuple[str, int]]:
    """Content AND modification time: a write of identical bytes is still a
    write, and against a live monitor it is a lost update."""
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(directory.iterdir()) if p.is_file()}


def test_no_tool_changes_or_adds_a_file(server, paths) -> None:
    state, _ = paths
    before = _fingerprint(state.parent)
    for name, args in [("get_status", {}), ("get_history", {"window": "30d"}),
                       ("get_trend", {}), ("get_history", {"window": "bad"})]:
        _call(server, name, args)
    assert _fingerprint(state.parent) == before


def test_the_server_never_opens_the_database_for_writing(server, monkeypatch) -> None:
    """mode=ro is what makes a write impossible rather than merely absent.
    Every connection the tools open goes through sqlite3.connect; none of them
    may be a plain path."""
    import sqlite3
    real = sqlite3.connect
    opened: list[str] = []

    def spy(target, *a, **kw):
        opened.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    _call(server, "get_history", {"window": "30d"})
    _call(server, "get_trend")
    assert opened, "no connection was observed"
    assert all("mode=ro" in t for t in opened), opened


# =============================================================================
# E. A real subprocess
# =============================================================================


def test_stdout_carries_only_protocol_lines(paths) -> None:
    state, db = paths
    env = {**os.environ, "STATE_FILE_PATH": str(state), "HISTORY_DB_PATH": str(db)}
    stdin = "\n".join([
        _req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "t", "version": "0"}}, id_=1),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        _req("tools/call", {"name": "get_status", "arguments": {}}, id_=2),
        _req("tools/call", {"name": "get_history", "arguments": {"window": "bad"}}, id_=3),
        _req("tools/call", {"name": "get_trend", "arguments": {}}, id_=4),
    ]) + "\n"
    proc = subprocess.run([sys.executable, str(ROOT / "mcp_server.py")],
                          input=stdin, capture_output=True, text=True,
                          env=env, cwd=str(state.parent), timeout=30)
    assert proc.returncode == 0, proc.stderr
    answers = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3, 4]
    assert URL in answers[1]["result"]["structuredContent"]["targets"]
    assert answers[2]["result"]["isError"] is True
    assert answers[3]["result"]["structuredContent"]["targets"][URL]["verdict"] == "degrading"
