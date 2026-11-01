"""
tests/test_probe_header_limit.py -- a long response header is not an outage.

From 23 to 24 October 2026 the scheduled collection recorded www.github.com
DOWN six times in a row while the site was answering 200. Its
Content-Security-Policy header had grown past 8190 bytes, aiohttp's default
`max_field_size`, and the client refused to parse the response:
"400, message='Got more than 8190 bytes when reading'". A browser accepts that
header without complaint, so the monitor paged on a limit of its own client,
not on anything the site did.

The probe session now accepts header fields up to PROBE_MAX_HEADER_FIELD_BYTES.
The limit still exists: a header far beyond it remains a probe failure, so a
misbehaving server cannot make the parser buffer without bound.

Coverage:
  A. A 16 KiB header (twice aiohttp's default) is UP with HTTP 200
  B. A header over the cap is still a probe failure, for that target alone
"""

from __future__ import annotations

import http.server
import os
import sys
import threading
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from main import Target

AIOHTTP_DEFAULT_FIELD_BYTES = 8190


# -----------------------------------------------------------------------------
# A local server. Loopback only: no test here reaches a real network.
# -----------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """/csp/<n>  200 with a Content-Security-Policy value of n bytes"""

    def do_GET(self) -> None:
        _, _, raw = self.path.strip("/").partition("/")
        try:
            self.send_response(200)
            self.send_header("Content-Security-Policy", "a" * int(raw))
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client gave up on the header: the over-the-cap case

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def quiet_round(monkeypatch):
    monkeypatch.setattr(main, "RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(main, "dispatch_alert", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_send_heartbeat", AsyncMock())

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)


async def _round(tmp_path, *urls: str) -> dict:
    """One real round through run_health_checks: the session under test is
    the one it builds, not one constructed here."""
    state_path = tmp_path / "state.json"
    await main.run_health_checks(
        targets=[Target(url=u) for u in urls],
        state_path=state_path, history_path=tmp_path / "h.db")
    return main.StateManager(state_path)._state


# =============================================================================
# A. A long header that a browser accepts
# =============================================================================


def test_the_cap_is_above_aiohttps_default() -> None:
    assert main.PROBE_MAX_HEADER_FIELD_BYTES > 2 * AIOHTTP_DEFAULT_FIELD_BYTES


async def test_a_16_kib_header_is_up_not_down(base, tmp_path, quiet_round) -> None:
    """Before the fix: DOWN, http_status None, 'Got more than 8190 bytes'."""
    url = f"{base}/csp/{16 * 1024}"

    state = await _round(tmp_path, url)

    assert state[url]["status"] in (main.STATUS_UP, main.STATUS_DEGRADED), (
        state[url].get("last_error"))
    assert state[url]["diagnostics"]["measured"]["http_status"] == 200


# =============================================================================
# B. The limit still exists
# =============================================================================


async def test_a_header_over_the_cap_is_a_probe_failure_for_that_target_alone(
        base, tmp_path, quiet_round) -> None:
    huge = f"{base}/csp/{main.PROBE_MAX_HEADER_FIELD_BYTES * 16}"
    ok   = f"{base}/csp/100"

    state = await _round(tmp_path, huge, ok)

    assert state[huge]["status"] == main.STATUS_DOWN
    assert state[ok]["status"] in (main.STATUS_UP, main.STATUS_DEGRADED)
