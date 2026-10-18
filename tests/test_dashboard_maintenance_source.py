"""
tests/test_dashboard_maintenance_source.py — the badge comes from the list the
monitor is actually running.

The dashboard computed its "silenced / window" badges from the store alone,
`read_store() or []`. While targets.yaml is the live list (no dashboard save
yet) that is an empty list: a target the monitor was muting showed no badge, so
a DOWN row looked like a DOWN row nobody had silenced. The monitor mutes from
the effective list -- the valid store if it exists, else the seed -- and the
page now asks the same question of the same source, through the same
in_maintenance function.

When that source cannot be read, "no window" would be a claim the page cannot
make, so the page says the maintenance state is unknown instead.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api
import diagnostics
import main
import targetstore

URL   = "https://m.example"
NIGHT = {"start": "02:00", "end": "04:00"}
BADGE = '<span class="muted-badge"'
UNKNOWN = "Maintenance windows are unknown"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """August 2026: the 3rd is a Monday."""
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """Freeze the API's idea of now; returns a setter."""
    holder = {"now": _at(3, 3)}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return holder["now"]

    monkeypatch.setattr(api, "datetime", Frozen)

    def set_now(moment: datetime) -> None:
        holder["now"] = moment

    return set_now


@pytest.fixture
def seed(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "targets.yaml"
    monkeypatch.setattr(targetstore, "SEED_FILE", path)
    monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "data" / "targets.json")
    return path


@pytest.fixture
def server(seed: Path, tmp_path: Path, clock):
    (tmp_path / "state.json").write_text(json.dumps({
        "schema_version": 2,
        "targets": {URL: {"status": "DOWN", "last_checked": "2026-08-03T03:00:00Z"}},
    }), encoding="utf-8")
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


def _page(base: str, route: str) -> str:
    with urllib.request.urlopen(base + route, timeout=30) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


ROUTES = ["/", "/partial/targets"]


def _seed_yaml(seed: Path, windows: list[dict] | None) -> None:
    lines = ["targets:", f"  - url: {URL}"]
    if windows:
        lines.append("    maintenance:")
        for window in windows:
            if "days" in window:
                lines.append(f"      - days: {window['days']}")
                lines.append(f"        start: '{window['start']}'")
            else:
                lines.append(f"      - start: '{window['start']}'")
            lines.append(f"        end: '{window['end']}'")
    seed.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _store(entries: list[dict]) -> None:
    targetstore.write_store(entries)


# =============================================================================
# A. The seed is the live list until the first save
# =============================================================================


@pytest.mark.parametrize("route", ROUTES)
def test_a_seed_window_in_effect_draws_the_badge(server, seed, clock, route) -> None:
    _seed_yaml(seed, [NIGHT])
    clock(_at(3, 3))

    page = _page(server, route)

    assert BADGE in page
    assert "02:00" in page and "04:00" in page
    assert UNKNOWN not in page


@pytest.mark.parametrize("route", ROUTES)
def test_a_seed_window_not_in_effect_draws_none(server, seed, clock, route) -> None:
    _seed_yaml(seed, [NIGHT])
    clock(_at(3, 5))

    page = _page(server, route)

    assert BADGE not in page
    assert UNKNOWN not in page


def test_a_seed_without_windows_draws_none(server, seed) -> None:
    _seed_yaml(seed, None)
    assert BADGE not in _page(server, "/")


# =============================================================================
# B. The store overrides the seed, and an empty store is still a store
# =============================================================================


@pytest.mark.parametrize("route", ROUTES)
def test_the_store_overrides_the_seed_windows(server, seed, clock, route) -> None:
    """The monitor stops reading the YAML the moment a store exists, so a window
    that only the YAML holds is not muting anything."""
    _seed_yaml(seed, [NIGHT])
    _store([{"url": URL}])
    clock(_at(3, 3))

    assert BADGE not in _page(server, route)


def test_a_store_window_is_drawn_although_the_seed_has_none(server, seed, clock) -> None:
    _seed_yaml(seed, None)
    _store([{"url": URL, "maintenance": [NIGHT]}])
    clock(_at(3, 3))

    assert BADGE in _page(server, "/")


def test_an_empty_store_is_not_replaced_by_the_seed(server, seed, clock) -> None:
    """[] is the operator's deliberate "monitor nothing"; the seed's window is
    not in effect for a list that has no targets."""
    _seed_yaml(seed, [NIGHT])
    _store([])
    clock(_at(3, 3))

    page = _page(server, "/")

    assert BADGE not in page
    assert UNKNOWN not in page


# =============================================================================
# C. A window that crosses midnight
# =============================================================================


@pytest.mark.parametrize("moment,inside", [
    (_at(3, 23), True),     # Monday 23:00
    (_at(4, 1),  True),     # Tuesday 01:00 is still Monday's window
    (_at(4, 3),  False),    # Tuesday 03:00: over
    (_at(5, 1),  False),    # Wednesday 01:00: Tuesday's window, not set
])
@pytest.mark.parametrize("source", ["seed", "store"])
def test_a_window_crossing_midnight(server, seed, clock, moment, inside, source) -> None:
    window = {"days": [0], "start": "22:00", "end": "02:00"}
    if source == "seed":
        _seed_yaml(seed, [window])
    else:
        _store([{"url": URL, "maintenance": [window]}])
    clock(moment)

    assert (BADGE in _page(server, "/")) is inside


# =============================================================================
# D. When the source cannot be read the page says "unknown", not "none"
# =============================================================================


@pytest.mark.parametrize("route", ROUTES)
def test_an_unreadable_store_makes_the_maintenance_state_unknown(
        server, seed, clock, route) -> None:
    _seed_yaml(seed, [NIGHT])                  # the seed must not be the answer
    targetstore.STORE_FILE.parent.mkdir(parents=True)
    targetstore.STORE_FILE.write_text("{not json", encoding="utf-8")
    clock(_at(3, 3))

    page = _page(server, route)

    assert UNKNOWN in page
    assert BADGE not in page, "the seed's window was drawn for a list the monitor refuses"


@pytest.mark.parametrize("route", ROUTES)
def test_an_unreadable_seed_with_no_store_makes_it_unknown(server, seed, route) -> None:
    seed.write_text("targets: [unclosed\n  - : :", encoding="utf-8")

    assert UNKNOWN in _page(server, route)


def test_the_unknown_notice_sits_inside_the_list_the_poll_replaces(
        server, seed) -> None:
    """The page polls /partial/targets and swaps #list; a notice outside it would
    be erased on the first poll, or never refreshed away once the store heals."""
    seed.write_text("targets: [unclosed\n  - : :", encoding="utf-8")

    page = _page(server, "/")

    assert page.index('<div id="list">') < page.index(UNKNOWN)


# =============================================================================
# E. One definition of "in a window"
# =============================================================================


def test_the_page_and_the_monitor_use_the_same_function() -> None:
    assert api.in_maintenance is diagnostics.in_maintenance
    assert main.in_maintenance is diagnostics.in_maintenance
