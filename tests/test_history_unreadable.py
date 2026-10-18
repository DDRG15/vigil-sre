"""
tests/test_history_unreadable.py — /history on a database it cannot read.

The history readers re-raise a read error on purpose: an unreadable database
is not an empty one. The request handler turns any sqlite3.Error into a JSON
500 for every route; this pins that the history page and its CSV export are
covered too, so a history.db that is not a SQLite file (a bad restore, a
truncated copy) answers with an error instead of a dropped connection or a page
that reads as "no data".
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import api


@pytest.fixture
def corrupt_server(tmp_path):
    db = tmp_path / "history.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    server = api.serve("127.0.0.1", 0, state_path=tmp_path / "state.json", history_path=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", ["/history", "/history.csv"])
def test_an_unreadable_history_is_an_error_not_a_page(corrupt_server, path):
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(corrupt_server + path, timeout=5)
    assert err.value.code == 500
    body = json.loads(err.value.read())
    assert body["error"] == "could not read monitoring data"
