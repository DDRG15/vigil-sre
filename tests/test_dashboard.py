"""
tests/test_dashboard.py — Test suite for dashboard.py (an earlier release).

Coverage:
  A. freshness()   — the four levels, and the boundaries between them
  B. _row()        — escaping, the three states, missing data
  C. render_page() — structure, and the dimming that makes stale data look stale
  D. HTTP routes   — / and /partial/targets served by api.py

Reviewer notes
--------------
Two tests carry most of the weight. test_a_hostile_url_is_escaped is the
security one: target URLs come from targets.yaml and land inside HTML, so an
unescaped one is stored XSS. And test_dead_data_dims_the_rows is the design
one — the whole point of the freshness banner is that green must stop looking
reassuring when it might be hours old, and that is a property of the rendered
page, not of a docstring.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard
from api import DEFAULT_HOST, serve
from dashboard import COLOURS, GLYPHS, RUN_INTERVAL_S, freshness, render_page, render_rows

URL = "https://example.com"
HOSTILE = 'https://x.com/<script>alert("pwned")</script>'


def _state(status: str = "UP", **extra) -> dict:
    base = {
        "status": status,
        "last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_error": "",
    }
    base.update(extra)
    return base


# =============================================================================
# A. freshness()
# =============================================================================


@pytest.mark.parametrize("seconds,level", [
    (0,                       "fresh"),
    (RUN_INTERVAL_S,          "fresh"),
    (RUN_INTERVAL_S * 2 - 1,  "fresh"),
    (RUN_INTERVAL_S * 2,      "stale"),    # two cycles is where it starts to matter
    (RUN_INTERVAL_S * 4,      "stale"),
    (RUN_INTERVAL_S * 5,      "dead"),     # five cycles: assume the prober died
    (RUN_INTERVAL_S * 100,    "dead"),
])
def test_freshness_levels_at_their_boundaries(seconds, level) -> None:
    """Tested on both sides of each threshold. A banner that fires is half a
    test; one that stays quiet just under its trigger is the other half."""
    assert freshness(seconds)[0] == level


def test_freshness_with_no_data_is_its_own_level() -> None:
    """'Never ran' and 'ran ages ago' are different statements. Collapsing
    them would tell a freshly deployed instance that its monitor is dead."""
    level, message = freshness(None)
    assert level == "unknown"
    assert "todavía" in message


def test_dead_freshness_says_what_it_means() -> None:
    _, message = freshness(RUN_INTERVAL_S * 20)
    assert "el pasado" in message


# =============================================================================
# B. _row() — escaping and states
# =============================================================================


def test_a_hostile_url_is_escaped(tmp_path: Path) -> None:
    """Target URLs come straight from targets.yaml into the HTML. Unescaped,
    a target named with a <script> tag is stored XSS against whoever opens
    the dashboard -- which is the operator, on the machine that runs the
    monitor."""
    markup = render_rows({HOSTILE: _state("DOWN")}, {}, stale_seconds=10)
    assert "<script>alert" not in markup
    assert "&lt;script&gt;" in markup


def test_a_hostile_error_message_is_escaped() -> None:
    """last_error is attacker-influenceable in a real sense: it can carry a
    server's response detail, and a malicious server controls that.

    What must be gone is the TAG, not the text: `onerror=` surviving as
    literal characters inside a <p> is both harmless and correct — the
    operator should see the error exactly as it arrived. It is the angle
    brackets and quotes that turn text into markup, and those are escaped."""
    hostile_error = '<img src=x onerror="alert(1)">'
    markup = render_rows({URL: _state("DOWN", last_error=hostile_error)}, {}, 10)
    assert "<img" not in markup          # no tag was ever created
    assert '"alert(1)"' not in markup    # nor an unescaped attribute value
    assert "&lt;img" in markup
    assert "&quot;alert(1)&quot;" in markup


@pytest.mark.parametrize("status", ["UP", "DEGRADED", "DOWN"])
def test_every_state_carries_colour_glyph_and_word(status) -> None:
    """Colour is never the only channel: roughly 8% of men have a colour
    vision deficiency and red/green is exactly the pair that fails. Each
    state must also be distinguishable by shape and by text."""
    markup = render_rows({URL: _state(status)}, {}, 10)
    assert COLOURS[status] in markup
    assert GLYPHS[status] in markup
    assert f"<b>{status}</b>" in markup


def test_unknown_status_does_not_crash_or_masquerade_as_up() -> None:
    """A hand-edited state.json can hold anything. An unrecognised status must
    render as UNKNOWN, never fall through to the reassuring colour."""
    markup = render_rows({URL: _state("BANANA")}, {}, 10)
    assert GLYPHS["UNKNOWN"] in markup
    assert COLOURS["UP"] not in markup


def test_missing_history_renders_a_dash_not_a_zero() -> None:
    """Zero percent claims the target was down all window; a target with no
    rows has made no such claim. The API already returns null -- the page
    must not turn that into a red 0%."""
    markup = render_rows({URL: _state()}, {URL: {}}, 10)
    assert "—" in markup
    assert "0.0%" not in markup


def test_a_non_dict_state_entry_does_not_break_the_page() -> None:
    """One malformed row must not cost the other five. The audit of an earlier release
    found api.read_state could surface a stray scalar; the renderer should
    survive it either way."""
    markup = render_rows({URL: "not a dict"}, {}, 10)  # type: ignore[dict-item]
    assert GLYPHS["UNKNOWN"] in markup


# =============================================================================
# C. render_page()
# =============================================================================


def test_page_is_self_contained() -> None:
    """No external stylesheet, script or font: the dashboard has to work on a
    host with no egress, and every third-party asset is one more party that
    learns when you look at your own outages."""
    page = render_page({URL: _state()}, {}, 10)
    for external in ("http://", "https://cdn", "//unpkg", "//cdnjs"):
        assert external not in page.replace(URL, "")


def test_dead_data_dims_the_rows() -> None:
    """The design decision of this phase, asserted as a property of the
    output. When the data may be hours old, green must stop looking
    reassuring -- so the row container carries the level that dims it."""
    fresh_page = render_page({URL: _state()}, {}, RUN_INTERVAL_S)
    dead_page  = render_page({URL: _state()}, {}, RUN_INTERVAL_S * 20)
    assert 'class="rows fresh"' in fresh_page
    assert 'class="rows dead"' in dead_page
    assert ".rows.dead{opacity" in dead_page   # the rule that does the dimming


def test_page_with_no_targets_says_so() -> None:
    page = render_page({}, {}, None)
    assert "No hay targets" in page


# =============================================================================
# D. HTTP routes
# =============================================================================


@pytest.fixture
def live_server(tmp_path: Path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "schema_version": 4,
        "targets": {URL: _state(), HOSTILE: _state("DOWN")},
    }), encoding="utf-8")
    server = serve(DEFAULT_HOST, 0, state_path=state,
                   history_path=tmp_path / "history.db")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{DEFAULT_HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, resp.headers, resp.read().decode("utf-8")


def test_root_serves_the_dashboard(live_server) -> None:
    status, headers, body = _get(live_server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "vigil-sre" in body
    assert URL in body


def test_partial_serves_only_the_rows(live_server) -> None:
    """The poll fetches a fragment, not the whole document -- otherwise every
    refresh would ship the stylesheet again."""
    _, _, page    = _get(live_server, "/")
    _, _, partial = _get(live_server, "/partial/targets")
    assert "<!doctype html>" in page.lower()
    assert "<!doctype html>" not in partial.lower()
    assert 'class="rows' in partial


def test_html_routes_send_a_restrictive_csp(live_server) -> None:
    """The page loads nothing external, so the policy can forbid everything
    external -- which also blunts anything that somehow slipped past escaping."""
    _, headers, _ = _get(live_server, "/")
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp


def test_hostile_target_is_escaped_over_the_wire(live_server) -> None:
    """End-to-end: the escaping has to survive to the actual response body,
    not just to a unit test of the renderer."""
    _, _, body = _get(live_server, "/")
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
