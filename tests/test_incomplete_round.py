"""
tests/test_incomplete_round.py — a round that did not finish must not say it did.

Why this is its own file
------------------------
Two different things could stop a round from being complete, and the monitor
reported both as success:

  - A probe raised something check_url did not anticipate. gather(
    return_exceptions=True) kept the other targets running, which is right, and
    then the run only added one to down_count -- the same bookkeeping as a
    target that is simply down. The success heartbeat went out, and without
    --strict the process exited 0. The watchdog (healthchecks.io) saw a monitor
    in perfect health; the target whose check crashed was not being watched.

  - StateManager._write_sync catches OSError and logs it. Correct for the other
    targets, whose alerts must still go out -- and then nothing downstream
    knew. Measured end to end: state.json never created, exit 0, heartbeat sent.
    Every later round starts from "no state", so every target looks new and the
    transition alerts it would have deduplicated are sent again, forever, while
    the watchdog stays green.

Both are the same sentence: "this run completed" was asserted without checking.
The decision, and the reason for each half of it:

  - A genuinely DOWN target is NOT an incomplete round. The heartbeat
    watches the monitor, not its targets; withholding it for a target outage
    would page "your monitor is dead" on top of the real incident.
  - The heartbeat is withheld, not sent to a /fail endpoint: the protocol is
    unchanged.
  - The process exits non-zero WITHOUT --strict, because --strict is about the
    targets and this is about the monitor.

Coverage:
  A. An unexpected exception in a probe
  B. A state write that fails (in-process, and a real process)
  C. A DOWN target is still a complete round
  D. The line the on-call reads
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import main
from main import Target


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers 200 to anything and records (method, path)."""

    hits: list = []

    def _ok(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).hits.append((self.command, self.path))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = do_POST = _ok

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    """A local 200-everything server; yields (base_url, hits)."""
    handler = type("Handler", (_Handler,), {"hits": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", handler.hits
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def crash_one_probe(monkeypatch):
    """check_url raises RuntimeError for any URL ending in /boom."""
    real = main.check_url

    async def _check(url, *args, **kwargs):
        if url.endswith("/boom"):
            raise RuntimeError("injected")
        return await real(url, *args, **kwargs)

    monkeypatch.setattr(main, "check_url", _check)


@pytest.fixture
def heartbeat(monkeypatch) -> AsyncMock:
    hb = AsyncMock()
    monkeypatch.setattr(main, "_send_heartbeat", hb)
    return hb


@pytest.fixture
def alerts(monkeypatch) -> AsyncMock:
    """Every alert 'delivered', so the exit code is not about delivery here."""
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "dispatch_alert", sent)
    return sent


# =============================================================================
# A. An unexpected exception in a probe
# =============================================================================


async def test_a_probe_that_crashes_makes_the_round_incomplete(
        tmp_path, server, crash_one_probe, heartbeat, alerts) -> None:
    """The healthy sibling still completes and is persisted -- gather() keeps
    that -- but the round as a whole is not clean, and the exit code says so
    without --strict. Before the fix it was 0: the crashed target simply was
    not watched this minute and nothing outside the process could tell."""
    base, _ = server
    state_path = tmp_path / "state.json"
    report = main.RunReport()

    down = await main.run_health_checks(
        targets=[Target(url=base + "/ok"), Target(url=base + "/boom")],
        state_path=state_path, history_path=tmp_path / "h.db", report=report)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))["targets"]
    assert base + "/ok" in persisted, "the healthy sibling must still complete"
    assert report.internal_errors == 1
    assert main._finish_run(False, down, state_path, report) != 0, (
        "a round with an internal probe crash exits 0")


async def test_a_probe_that_crashes_withholds_the_success_heartbeat(
        tmp_path, server, crash_one_probe, heartbeat, alerts) -> None:
    base, _ = server
    await main.run_health_checks(
        targets=[Target(url=base + "/ok"), Target(url=base + "/boom")],
        state_path=tmp_path / "state.json", history_path=tmp_path / "h.db")
    assert heartbeat.await_count == 0, f"heartbeat sent {heartbeat.await_args_list}"


# =============================================================================
# B. A state write that fails
# =============================================================================


def _block_state_writes(state_path: Path) -> None:
    """A directory where the tmp file should go: write_text raises OSError."""
    state_path.with_suffix(".tmp").mkdir()


async def test_a_failed_state_write_is_counted_and_does_not_raise(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    _block_state_writes(state_path)
    state = main.StateManager(state_path)
    assert state.write_failures == 0

    await state.set_up("https://a.example")          # must not raise

    assert state.write_failures == 1
    assert not state_path.exists()


async def test_a_failed_state_write_withholds_the_heartbeat_but_not_the_alerts(
        tmp_path, server, heartbeat, alerts, monkeypatch) -> None:
    """_write_sync still does not raise: the alert for the SECOND target has to
    go out even though the first one's state could not be saved. What changes is
    that the round knows, and refuses to call itself complete.

    Both targets fail on their first sighting. A healthy first sighting sends
    nothing any more (see test_first_sight.py), so it cannot carry this test."""
    async def _probe(*a, **k):
        raise main._ProbeFailure("HTTP 503 (expected 200)")

    async def _no_sample(*a, **k):
        return None

    monkeypatch.setattr(main, "_probe_with_backoff", _probe)
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    base, _ = server
    state_path = tmp_path / "state.json"
    _block_state_writes(state_path)
    report = main.RunReport()

    down = await main.run_health_checks(
        targets=[Target(url=base + "/a"), Target(url=base + "/b")],
        state_path=state_path, history_path=tmp_path / "h.db", report=report)

    assert alerts.await_count == 2, "each target's first-observation alert must still go out"
    assert report.state_write_failures > 0
    assert heartbeat.await_count == 0
    assert main._finish_run(False, down, state_path, report) != 0


def _run_main_with_blocked_state(tmp_path: Path, server_url: str, hb_url: str
                                 ) -> subprocess.CompletedProcess:
    """A real `python main.py`, cwd in a temp dir, state.json unwritable."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "targets.yaml").write_text(
        f"targets:\n  - {server_url}/ok\n", encoding="utf-8")
    (work / "state.tmp").mkdir()          # tmp.write_text() -> OSError
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DISCORD", "SLACK", "HEARTBEAT"))}
    env.update(
        PYTHONPATH=str(REPO),
        PYTHONIOENCODING="utf-8",
        DISCORD_WEBHOOK_URL="http://127.0.0.1:9/never",
        STATE_FILE_PATH=str(work / "state.json"),
        HEARTBEAT_URL=hb_url,
        TARGETS_STORE_PATH=str(work / "nostore.json"),
    )
    return subprocess.run(
        [sys.executable, str(REPO / "main.py")], cwd=work, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


@pytest.fixture
def blocked_state_run(tmp_path):
    """One real process, shared by the e2e assertions: (process, heartbeat hits)."""
    target = type("H", (_Handler,), {"hits": []})
    hb     = type("H", (_Handler,), {"hits": []})
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (target, hb)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proc = _run_main_with_blocked_state(
            tmp_path,
            f"http://127.0.0.1:{servers[0].server_address[1]}",
            f"http://127.0.0.1:{servers[1].server_address[1]}/ping")
        yield proc, hb.hits, tmp_path / "work"
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()


def test_state_write_failure_e2e_exits_non_zero(blocked_state_run) -> None:
    """state.json never persisted. Before the fix: exit 0, because
    undelivered_alerts() reads the file that does not exist and finds nothing
    pending."""
    proc, _, work = blocked_state_run
    assert not (work / "state.json").exists()
    assert proc.returncode != 0, (
        f"state never persisted yet process exits {proc.returncode}\n{proc.stdout[-1200:]}")


def test_state_write_failure_e2e_sends_no_success_heartbeat(blocked_state_run) -> None:
    _, hb_hits, _ = blocked_state_run
    pings = [h for h in hb_hits if h[0] == "POST"]
    assert not pings, f"success heartbeat sent although state was not persisted: {pings}"


# =============================================================================
# C. A DOWN target is still a complete round
# =============================================================================


async def test_a_down_target_still_sends_the_heartbeat_and_exits_zero(
        tmp_path, monkeypatch, heartbeat, alerts) -> None:
    """The watchdog watches the monitor, not its targets. A confirmed-DOWN
    target is this monitor's normal operating condition: heartbeat sent, exit 0
    without --strict, exit 1 with it."""
    async def _probe(*a, **k):
        raise main._ProbeFailure("HTTP 503 (expected 200)")

    async def _no_sample(*a, **k):
        return None

    monkeypatch.setattr(main, "_probe_with_backoff", _probe)
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    state_path = tmp_path / "state.json"
    report = main.RunReport()

    down = await main.run_health_checks(
        targets=[Target(url="https://down.example")],
        state_path=state_path, history_path=tmp_path / "h.db", report=report)

    assert down == 1
    assert report.internal_errors == 0 and report.state_write_failures == 0
    assert heartbeat.await_count == 1
    assert main._finish_run(False, down, state_path, report) == 0
    assert main._finish_run(True, down, state_path, report) == 1


# =============================================================================
# D. The line the on-call reads
# =============================================================================


async def test_an_incomplete_round_logs_one_error_naming_the_condition_and_counts(
        tmp_path, server, crash_one_probe, heartbeat, alerts, caplog) -> None:
    """One line, not a trail to reconstruct: what happened, how many, what was
    done about it, and a metric name an alert can key on."""
    base, _ = server
    state_path = tmp_path / "state.json"
    report = main.RunReport()

    with caplog.at_level("ERROR", logger="sre.health_checker"):
        down = await main.run_health_checks(
            targets=[Target(url=base + "/ok"), Target(url=base + "/boom")],
            state_path=state_path, history_path=tmp_path / "h.db", report=report)
        main._finish_run(False, down, state_path, report)

    lines = [r.getMessage() for r in caplog.records
             if "rounds_incomplete_total" in r.getMessage()]
    assert len(lines) == 1, lines
    assert all(r.levelname == "ERROR" for r in caplog.records
               if "rounds_incomplete_total" in r.getMessage())
    line = lines[0]
    assert "internal_errors=1" in line and "state_write_failures=0" in line
    assert "heartbeat" in line.lower() and "non-zero" in line.lower()
    assert "event_type=metric metric=rounds_incomplete_total value=1" in line


async def test_a_complete_round_logs_no_incomplete_line(
        tmp_path, server, heartbeat, alerts, caplog) -> None:
    base, _ = server
    report = main.RunReport()
    with caplog.at_level("ERROR", logger="sre.health_checker"):
        await main.run_health_checks(
            targets=[Target(url=base + "/ok")],
            state_path=tmp_path / "state.json", history_path=tmp_path / "h.db",
            report=report)
    assert not [r for r in caplog.records if "rounds_incomplete_total" in r.getMessage()]
    assert heartbeat.await_count == 1
