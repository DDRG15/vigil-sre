"""
tests/test_api_sigterm.py — `docker compose stop` must not SIGKILL the dashboard.

api.py runs as PID 1 of its container. The kernel ignores a signal PID 1 has no
handler for, and api.py handled only Ctrl+C (KeyboardInterrupt), so SIGTERM did
nothing: compose waited its grace period and sent SIGKILL, and every stop ended
in exit 137. Outside a container the same gap shows as death by signal (-15)
instead of a clean exit, which is what this test can observe without being PID 1.

POSIX only: on Windows a "SIGTERM" to a child process is TerminateProcess, which
no handler can intercept.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

API = Path(__file__).resolve().parent.parent / "api.py"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="needs POSIX signals")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"api.py exited early with {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("api.py never started listening")


def test_sigterm_stops_the_api_cleanly(tmp_path):
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "API_PORT": str(port),
        "STATE_FILE_PATH": str(tmp_path / "state.json"),
        "HISTORY_DB_PATH": str(tmp_path / "history.db"),
    })
    proc = subprocess.Popen(
        [sys.executable, str(API)], cwd=tmp_path, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(port, proc)
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert code == 0, f"SIGTERM ended api.py with {code}, not a clean exit 0"
