"""
tests/test_editor_state.py -- the target editor must not save a list it never loaded.

What is actually being defended
-------------------------------
Save sends the WHOLE list and the API replaces whatever is stored with it. The
editor used to abandon a failed first load (network, HTTP error, bad JSON) and
leave Save enabled on an empty table. An operator who then added one row and
saved replaced every monitored target with that one row, and the success
message said so cheerfully.

The editor is therefore a small state machine -- idle, loading, loaded, failed --
and Save exists only in `loaded`. These tests drive the shipped script
(dashboard._EDITOR_SCRIPT) in Node against a DOM stub, because the property is
about ordering and timing, which a text search of the source cannot see. Where
Node is absent they skip; CI's runner has it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dashboard  # noqa: E402

HARNESS = ROOT / "tests" / "editor_harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """Every scenario once; each test reads its own slice."""
    script = tmp_path_factory.mktemp("editor") / "editor.js"
    script.write_text(dashboard._EDITOR_SCRIPT, encoding="utf-8")
    done = subprocess.run(
        ["node", str(HARNESS), str(script)],
        capture_output=True, text=True, timeout=60, encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    results = json.loads(done.stdout)
    broken = {k: v["error"] for k, v in results.items() if "error" in v}
    assert not broken, f"scenarios crashed in the harness: {broken}"
    return results


# =============================================================================
# A. Save does not exist until a valid snapshot has arrived
# =============================================================================


def test_save_and_add_start_disabled_in_the_served_markup() -> None:
    """Before the first open there is no list. If the script never runs (blocked,
    refused by CSP, a syntax error in a later edit) the buttons must already be
    inert, because the markup is all that is left."""
    page = dashboard.render_page({}, {}, None)
    for button in ("eadd", "esave"):
        tag = re.search(rf'<button[^>]*id="{button}"[^>]*>', page)
        assert tag, f"no #{button} button in the page"
        assert re.search(r"\sdisabled(\s|>|=)", tag.group(0)), (
            f"#{button} is enabled before any list was loaded"
        )


def test_nothing_is_editable_before_the_first_open(run) -> None:
    assert run["initial"]["save"] is True
    assert run["initial"]["add"] is True


def test_save_stays_disabled_while_the_first_load_is_in_flight(run) -> None:
    view = run["loading"]
    assert view["fetches"] == 1
    assert view["save"] is True
    assert view["add"] is True


@pytest.mark.parametrize("scenario", [
    "http_error", "network_error", "not_json", "shape_missing_targets",
    "shape_targets_not_a_list", "shape_null_entry", "shape_entry_without_url",
])
def test_every_kind_of_failed_load_leaves_save_disabled_and_says_why(run, scenario) -> None:
    """Five ways a load fails, and the two ways an answer that parses is still
    not a list. None may leave Save enabled, and none may stay silent."""
    view = run[scenario]
    assert view["save"] is True, "Save is enabled over a list that never loaded"
    assert view["add"] is True, "rows can be added to a table that is not the list"
    assert view["urls"] == []
    assert view["msgClass"] == "bad"
    assert "Could not load" in view["msg"]


def test_the_http_status_is_part_of_the_message(run) -> None:
    assert "500" in run["http_error"]["msg"]


def test_a_refusal_from_the_api_shows_its_reason_and_keeps_save_disabled(run) -> None:
    """A store that exists but cannot be used makes GET answer 500 with `error`
    and `store_error` and no list. An editor that says only "could not load"
    leaves the operator to guess; one that stays silent opens an empty table and
    invites exactly the save this file exists to prevent."""
    view = run["http_500_with_a_reason"]
    assert view["save"] is True and view["add"] is True
    assert view["msgClass"] == "bad"
    assert "500" in view["msg"]
    assert "data/targets.json is present but unusable" in view["msg"]
    assert "invalid JSON at line 3" in view["msg"]


def test_an_unreadable_error_body_still_yields_the_status(run) -> None:
    view = run["http_500_with_a_non_json_body"]
    assert view["save"] is True
    assert "500" in view["msg"] and "Could not load" in view["msg"]


def test_a_reason_that_is_not_text_is_not_painted(run) -> None:
    """The reason is server text written with textContent, so markup in it stays
    text; a store_error that is not a string is dropped instead of printed."""
    view = run["http_500_with_a_hostile_reason"]
    assert view["save"] is True
    assert "<img src=x onerror=alert(1)>" in view["msg"], "the reason was not shown as text"
    assert "42" not in view["msg"]


def test_a_superseded_refusal_is_not_painted(run) -> None:
    view = run["stale_500_reason_is_not_painted"]
    assert view["save"] is False
    assert "old failure" not in view["msg"] and "old reason" not in view["msg"]


def test_an_error_body_that_arrives_after_a_newer_load_began_is_dropped(run) -> None:
    """The check after `fetch` is not enough on its own: reading the error body
    is a second await, and a newer load can start inside it."""
    scenario = run["refusal_body_arrives_after_a_newer_load_started"]
    during, after = scenario["during"], scenario["after"]
    assert "old failure" not in during["msg"] and "old reason" not in during["msg"]
    assert during["save"] is True, "the superseded failure changed the state it no longer owns"
    assert after["save"] is False
    assert after["urls"] == ["https://newer.example"]
    assert after["msg"] == ""


def test_a_refused_save_shows_the_reason_too(run) -> None:
    view = run["save_refused_with_a_reason"]
    assert view["msgClass"] == "bad"
    assert "data/targets.json is present but unusable" in view["msg"]
    assert "invalid JSON at line 3" in view["msg"]
    assert view["save"] is False, "a refused save is not a failed load: the edits stay"


def test_the_original_defect_cannot_be_reproduced(run) -> None:
    """The exact sequence: first load fails, the operator adds a row, saves."""
    view = run["failed_load_then_add_and_save"]
    assert view["addClicked"] is False
    assert view["saveClicked"] is False
    assert view["urls"] == []
    assert view["puts"] == 0, (
        f"a PUT replaced the stored list with an unloaded table: {view['putBodies']}"
    )


def test_a_valid_snapshot_enables_editing_and_clears_the_loading_message(run) -> None:
    during, after = run["success"]["during"], run["success"]["after"]
    assert during["save"] is True and during["add"] is True
    assert after["save"] is False
    assert after["add"] is False
    assert after["urls"] == ["https://a.example", "https://b.example"]
    assert after["controlsEnabled"] is True
    assert after["msg"] == ""


def test_a_retry_after_a_failure_recovers_and_drops_the_error(run) -> None:
    scenario = run["retry_recovers"]
    assert scenario["failed"]["save"] is True
    assert scenario["retrying"]["save"] is True, "Save came back before the retry landed"
    assert scenario["retrying"]["fetches"] == 2
    recovered = scenario["recovered"]
    assert recovered["save"] is False
    assert recovered["urls"] == ["https://a.example"]
    assert "Could not" not in recovered["msg"], "a stale error outlived the success"


def test_a_failed_reload_blocks_save_even_though_rows_are_still_on_screen(run) -> None:
    """The rows left over from the earlier load may not be the server's list any
    more. A reload that fails is a list the page cannot vouch for."""
    view = run["reload_failure_blocks_save"]
    assert view["save"] is True
    assert view["controlsDisabled"] is True


# =============================================================================
# B. Out-of-order answers
# =============================================================================


def test_an_older_success_arriving_after_a_newer_one_is_ignored(run) -> None:
    view = run["stale_success_after_newer"]
    assert view["urls"] == ["https://newer.example"], (
        "an older response overwrote a newer one"
    )
    assert view["save"] is False


def test_an_older_failure_arriving_after_a_newer_success_is_ignored(run) -> None:
    view = run["stale_failure_after_newer"]
    assert view["save"] is False, "a stale failure disabled a list that did load"
    assert view["urls"] == ["https://newer.example"]
    assert "Could not" not in view["msg"]


def test_an_older_success_does_not_enable_save_while_the_newer_load_is_pending(run) -> None:
    view = run["stale_success_while_newer_pending"]
    assert view["save"] is True
    assert view["urls"] == [], "a superseded answer was painted"


def test_an_older_failure_does_not_poison_the_newer_load(run) -> None:
    scenario = run["stale_failure_does_not_poison_newer"]
    assert scenario["between"]["msgClass"] != "bad", "a superseded failure was reported"
    assert scenario["after"]["save"] is False
    assert scenario["after"]["urls"] == ["https://newer.example"]


# =============================================================================
# C. A load never clobbers edits in progress
# =============================================================================


def test_reopening_with_unsaved_edits_keeps_them_and_asks_the_server_nothing(run) -> None:
    """Closing and reopening the panel used to refetch and repaint, discarding
    whatever had been typed. With edits on screen there is nothing to load."""
    view = run["edits_survive_a_reopen"]
    assert view["fetches"] == 1, "the reopen refetched over unsaved edits"
    assert view["urls"] == ["https://a-edited.example", "https://b.example", ""]
    assert view["save"] is False


def test_reopening_a_clean_editor_reloads_it_and_locks_it_meanwhile(run) -> None:
    during, after = run["clean_reopen_reloads"]["during"], run["clean_reopen_reloads"]["after"]
    assert during["fetches"] == 2
    assert during["save"] is True
    assert during["controlsDisabled"] is True, (
        "rows are editable while the answer that will replace them is in flight"
    )
    assert after["urls"] == ["https://a.example", "https://c.example"]
    assert after["save"] is False


def test_a_saved_list_counts_as_clean_so_the_next_open_reloads_it(run) -> None:
    """After a successful save the table equals the server's list. If it still
    counted as edited, the reopen would never refresh it."""
    view = run["saved_list_reloads_on_reopen"]
    assert view["fetches"] == 3, "GET, PUT, then a reload on reopen"


# =============================================================================
# D. What already worked still works
# =============================================================================


def test_a_loaded_list_is_what_save_sends(run) -> None:
    view = run["save_sends_the_loaded_list"]
    assert view["putBodies"] == [{"targets": [
        {"url": "https://a.example", "remind": False, "trend_alerts": True}]}]


def test_a_legitimately_empty_list_still_needs_the_explicit_confirmation(run) -> None:
    declined, confirmed = run["empty_list_declined"], run["empty_list_confirmed"]
    assert declined["confirms"] == 1 and declined["puts"] == 0
    assert confirmed["confirms"] == 1
    assert confirmed["putBodies"] == [{"targets": [], "allow_empty": True}]
