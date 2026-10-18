"""
tests/test_ci_receiver.py — the webhook sink the CI end-to-end cycle posts to.

Why this file exists
--------------------
The CI job proves delivery by counting what the sink received. A sink that
answers 200 instead of 204, or that logs nothing, turns "at least one POST"
into a number that means nothing -- and the job cannot tell, because it only
reads the count. So the sink is held to the three things the job relies on:
it answers a POST with 204 (what Discord answers), it records every POST, and
a readiness probe does not count as a delivery.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci-webhook-receiver.py"


@pytest.fixture
def receiver(tmp_path):
    """The real script, as a process, on a free port: (base_url, log_path)."""
    log = tmp_path / "hits.ndjson"
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--host", "127.0.0.1", "--port", "0",
         "--log", str(log)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        banner = proc.stdout.readline()
        assert "listening on" in banner, f"no banner from the receiver: {banner!r}"
        port = int(banner.strip().rsplit(":", 1)[1])
        yield f"http://127.0.0.1:{port}", log
    finally:
        proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def _lines(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_post_is_answered_204_and_recorded(receiver) -> None:
    base, log = receiver
    body = json.dumps({"content": "alert"}).encode()

    assert _post(base + "/hook", body) == 204

    hits = _lines(log)
    assert len(hits) == 1
    assert hits[0]["path"] == "/hook"
    assert json.loads(hits[0]["body"]) == {"content": "alert"}


def test_every_post_is_one_line(receiver) -> None:
    """The job counts with `wc -l`: a body spanning lines would inflate it."""
    base, log = receiver
    _post(base + "/hook", b'{"a": 1,\n "b": 2}')
    _post(base + "/hook", b"{}")

    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_the_readiness_probe_is_not_a_delivery(receiver) -> None:
    """The job polls GET / until the sink is up. If that counted, a monitor
    that delivered nothing would still show one hit and the job would pass."""
    base, log = receiver

    with urllib.request.urlopen(base + "/", timeout=10) as response:
        assert response.status == 200

    assert _lines(log) == []
