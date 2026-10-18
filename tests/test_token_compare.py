"""
tests/test_token_compare.py — a non-ASCII token is a wrong token, not a crash.

hmac.compare_digest raises TypeError when either argument is a str holding a
non-ASCII character. The supplied header is attacker-controlled, so
`X-Vigil-Token: café` killed the handler thread: no 401, a reset connection,
and a traceback in the log for anyone who can reach the port. Both sides are now
compared as UTF-8 bytes, which compare_digest accepts for any content.

http.server decodes header bytes as Latin-1, so a header can only carry
characters up to U+00FF; the tokens below stay inside that range because a
client cannot send anything else.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api
import targetstore

ASCII_TOKEN  = "plain-ascii-token-not-a-secret"
UNICODE_TOKEN = "tökén-ñ-not-a-secret"      # tökén-ñ


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "data" / "targets.json")
    srv = api.serve(api.DEFAULT_HOST, 0, state_path=tmp_path / "state.json",
                    history_path=tmp_path / "history.db")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{api.DEFAULT_HOST}:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _put(base: str, token: str):
    request = urllib.request.Request(
        base + "/api/targets", method="PUT",
        data=json.dumps({"targets": [{"url": "https://ok.example"}]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Vigil-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


@pytest.mark.parametrize("configured", [ASCII_TOKEN, UNICODE_TOKEN])
def test_a_non_ascii_wrong_token_is_a_401(server, monkeypatch, configured) -> None:
    monkeypatch.setenv("API_WRITE_TOKEN", configured)

    assert _put(server, "café") == 401


def test_the_server_keeps_answering_after_a_non_ascii_token(server, monkeypatch) -> None:
    """The dead handler thread was the visible symptom; the next request has to
    be served normally."""
    monkeypatch.setenv("API_WRITE_TOKEN", ASCII_TOKEN)

    assert _put(server, "café") == 401
    assert _put(server, ASCII_TOKEN) == 200


def test_a_correct_non_ascii_token_is_accepted(server, monkeypatch) -> None:
    monkeypatch.setenv("API_WRITE_TOKEN", UNICODE_TOKEN)

    assert _put(server, UNICODE_TOKEN) == 200


def test_a_token_that_differs_only_in_a_non_ascii_character_is_refused(
        server, monkeypatch) -> None:
    monkeypatch.setenv("API_WRITE_TOKEN", UNICODE_TOKEN)

    assert _put(server, UNICODE_TOKEN.replace("ö", "o")) == 401


def test_an_ascii_token_still_works(server, monkeypatch) -> None:
    monkeypatch.setenv("API_WRITE_TOKEN", ASCII_TOKEN)

    assert _put(server, ASCII_TOKEN) == 200
    assert _put(server, ASCII_TOKEN + "x") == 401
