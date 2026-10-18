"""
tests/test_api_dotenv.py — `python api.py` reads the .env beside it.

main.py and targetctl.py call load_dotenv; api.py did not. Run standalone, as
the README tells a non-Docker user to, it therefore ignored API_WRITE_TOKEN in
.env: every write answered 401 while the editor blamed the .env, which held the
right value. The paths had the same hole: targetstore.STORE_FILE and
history.HISTORY_DB_FILE are fixed when their modules are imported, so reading
.env afterwards would have changed the token and nothing else.

Each test runs api.py as a real child process in a temp directory holding a fake
.env. Nothing here is a real secret.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import history

DOTENV_TOKEN = "token-from-dotenv-not-a-secret"
ENV_TOKEN    = "token-from-environment-not-a-secret"
URL          = "https://dotenv.example"

#: Variables the child must start without, so the only source is what the test
#: gives it.
_SCRUBBED = ("API_WRITE_TOKEN", "API_HOST", "API_PORT", "TARGETS_STORE_PATH",
             "HISTORY_DB_PATH", "STATE_FILE_PATH", "VIGIL_API")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env.update(extra)
    return env


def _request(port: int, path: str, method: str = "GET",
             body: dict | None = None, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Vigil-Token"] = token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method, headers=headers,
        data=None if body is None else json.dumps(body).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class _Api:
    """`python api.py` in *cwd*, with a log file to show if it dies."""

    def __init__(self, cwd: Path, env: dict[str, str], port: int) -> None:
        self.port = port
        self.log = cwd / "api-child.log"
        self._handle = self.log.open("wb")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "api.py")], cwd=cwd, env=env,
            stdout=self._handle, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                _request(port, "/api/targets")
                return
            except OSError:
                time.sleep(0.2)
        self.stop()
        pytest.fail(f"api.py did not start: {self.log.read_text(errors='replace')}")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._handle.close()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


def _dotenv(work: Path, port: int | None = None, token: str = DOTENV_TOKEN) -> None:
    """A fake .env. The port is left out unless a test is about the port: with
    it in the file, a child that ignores .env listens on 8787 and the test dies
    on a startup timeout instead of on the assertion that names the defect."""
    port_line = "" if port is None else f"API_PORT={port}\n"
    (work / ".env").write_text(
        f"API_WRITE_TOKEN={token}\n"
        f"{port_line}"
        f"TARGETS_STORE_PATH={work / 'custom' / 'targets.json'}\n"
        f"HISTORY_DB_PATH={work / 'custom' / 'history.db'}\n"
        f"STATE_FILE_PATH={work / 'custom' / 'state.json'}\n",
        encoding="utf-8",
    )


@pytest.fixture
def started(workdir: Path):
    """Start api.py against workdir; yields a factory so a test picks the env."""
    running: list[_Api] = []

    def start(env: dict[str, str], port: int) -> _Api:
        api_proc = _Api(workdir, env, port)
        running.append(api_proc)
        return api_proc

    yield start
    for proc in running:
        proc.stop()


def test_the_token_in_dotenv_is_honoured(workdir, started) -> None:
    port = _free_port()
    _dotenv(workdir)
    started(_child_env(API_PORT=str(port)), port)

    status, body = _request(port, "/api/targets")
    assert status == 200 and body["writable"] is True, (
        "the editor reports writing disabled although .env holds the token")

    status, _ = _request(port, "/api/targets", "PUT",
                         {"targets": [{"url": URL}]}, token=DOTENV_TOKEN)
    assert status == 200


def test_the_store_path_in_dotenv_is_where_the_write_lands(workdir, started) -> None:
    """STORE_FILE is fixed at import; loading .env afterwards must still move it."""
    port = _free_port()
    _dotenv(workdir)
    started(_child_env(API_PORT=str(port)), port)

    status, _ = _request(port, "/api/targets", "PUT",
                         {"targets": [{"url": URL}]}, token=DOTENV_TOKEN)

    assert status == 200
    saved = json.loads((workdir / "custom" / "targets.json").read_text(encoding="utf-8"))
    assert [e["url"] for e in saved["targets"]] == [URL]
    assert not (workdir / "data" / "targets.json").exists()


def test_the_history_path_in_dotenv_is_the_database_that_is_read(workdir, started) -> None:
    port = _free_port()
    _dotenv(workdir)
    (workdir / "custom").mkdir()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (workdir / "custom" / "state.json").write_text(json.dumps({
        "schema_version": 2,
        "targets": {URL: {"status": "UP", "last_checked": now}},
    }), encoding="utf-8")
    con = sqlite3.connect(workdir / "custom" / "history.db")
    con.executescript(history.SCHEMA)
    con.execute(
        "INSERT INTO probe_results (run_started_at, url, checked_at, status, ttfb_ms) "
        "VALUES (?, ?, ?, 'UP', 10.0)", (now, URL, now))
    con.commit()
    con.close()
    started(_child_env(API_PORT=str(port)), port)

    status, body = _request(port, "/api/history?window=7d")

    assert status == 200
    assert body["targets"][URL]["uptime_pct"] == 100.0, (
        "the API read the default history.db, not the one .env names")


def test_the_port_in_dotenv_is_the_port_it_listens_on(workdir, started) -> None:
    port = _free_port()
    _dotenv(workdir, port)
    started(_child_env(), port)    # _Api fails the test if nothing answers there

    assert _request(port, "/api/targets")[0] == 200


def test_the_environment_wins_over_dotenv(workdir, started) -> None:
    port = _free_port()
    _dotenv(workdir)
    started(_child_env(API_WRITE_TOKEN=ENV_TOKEN, API_PORT=str(port)), port)

    wrong, _ = _request(port, "/api/targets", "PUT",
                        {"targets": [{"url": URL}]}, token=DOTENV_TOKEN)
    right, _ = _request(port, "/api/targets", "PUT",
                        {"targets": [{"url": URL}]}, token=ENV_TOKEN)

    assert (wrong, right) == (401, 200)


def test_importing_api_does_not_read_dotenv(workdir) -> None:
    """Only the entry point loads it. A test (or a tool) that imports api must
    not have its environment rewritten by whatever .env sits in the cwd."""
    _dotenv(workdir, 1)
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import api, os; "
         "print(os.environ.get('API_WRITE_TOKEN'))", str(ROOT)],
        cwd=workdir, env=_child_env(), capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "None"
