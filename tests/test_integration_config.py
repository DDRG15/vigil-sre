"""
tests/test_integration_config.py — from the stored config to the running probe.

The gap this closes
-------------------
The maintenance-window feature shipped completely inert. The store validated
the window, `in_maintenance` evaluated it correctly, the alert funnel honoured
it, and the dashboard drew the badge — and in production it did nothing,
because `load_targets` built `Target` objects without the field. Two subsystems
disagreed about reality and only the visible one was right: the operator read
"silenced" on the page and got paged anyway.

**491 tests passed while that was true.** Every one of them constructed
`Target(...)` by hand, so none crossed the seam where the field was dropped.

The obvious repair — a test per field — has the same hole for the NEXT field.
So these walk the store's own schema instead: every key `targetstore` accepts
must survive the whole path from JSON on disk to the keyword arguments
`check_url` actually receives. A field added to the store and forgotten in
`load_targets` fails here without anybody remembering to write a test for it.

That is the difference between a test that catches this bug and one that
catches this bug's family.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
import targetstore

URL = "https://example.com/health"

#: One value per store field, all distinguishable from any default, so an
#: assertion cannot pass by coincidence.
FULL_ENTRY: dict = {
    "url"             : URL,
    "expected_status" : 204,
    "timeout_s"       : 12.5,
    "degraded_ttfb_ms": 3210.0,
    "degraded_rtt_ms" : 456.0,
    "degraded_backend_ms": 1789.0,
    "remind"          : True,
    "trend_alerts"    : False,   # distinguishable from the True default
    "maintenance"     : [{"days": [0, 6], "start": "02:00", "end": "04:00"}],
}

#: Fields that reach the Target but deliberately never reach check_url.
#:
#: Until trend_alerts, every store field was a per-probe setting, so "reaches
#: the Target" and "reaches check_url" were the same seam and this file only
#: needed one map. trend_alerts is consumed once per CYCLE by
#: _maybe_alert_trend, after history is recorded -- a 7-day trend does not
#: change between hourly probes, and check_url has no history_path in scope.
#:
#: Listing it here rather than in FIELD_TO_KWARG keeps the coverage guard
#: honest: the field is still accounted for, still asserted to survive the
#: load, and its effect on the alert is asserted as an outcome in
#: tests/test_trend_alert.py. What it is NOT is silently unchecked, which is
#: the omission this file exists to catch.
FIELDS_BEYOND_THE_PROBE: frozenset[str] = frozenset({"trend_alerts"})

#: Store key -> the check_url keyword it must arrive as. `url` travels
#: positionally; `expect_substring` is resolved on the way through (a literal
#: passes unchanged) so it is checked separately.
FIELD_TO_KWARG: dict[str, str] = {
    "expected_status" : "expected_status",
    "timeout_s"       : "timeout_s",
    "degraded_ttfb_ms": "degraded_ttfb_ms",
    "degraded_rtt_ms" : "degraded_rtt_ms",
    "degraded_backend_ms": "degraded_backend_ms",
    "remind"          : "remind",
    "maintenance"     : "maintenance",
}


@pytest.fixture
def configured(tmp_path: Path):
    """A store on disk holding one fully-specified target, plus a YAML seed
    that says something DIFFERENT — so a test cannot pass by accidentally
    reading the fallback."""
    yaml_path = tmp_path / "targets.yaml"
    yaml_path.write_text(
        "targets:\n  - https://seed-should-not-be-read.example\n", encoding="utf-8")
    store_path = tmp_path / "targets.json"
    targetstore.write_store([targetstore.validate_entry(FULL_ENTRY)], store_path)
    return yaml_path, store_path


# =============================================================================
# A. The store reaches the Target
# =============================================================================


def test_every_store_field_is_mapped_by_this_test(tmp_path: Path) -> None:
    """The test's own coverage, asserted.

    A field added to ALLOWED_KEYS and not to FIELD_TO_KWARG would be silently
    unchecked here — the same shape of omission this file exists to catch, one
    level up. So the mapping has to stay complete or this fails first.
    """
    accounted = (
        set(FIELD_TO_KWARG) | set(FIELDS_BEYOND_THE_PROBE)
        | {"url", "expect_substring"}
    )
    missing   = targetstore.ALLOWED_KEYS - accounted
    assert not missing, (
        f"store accepts {sorted(missing)} but this test does not follow it "
        "through — add it to FIELD_TO_KWARG, or to FIELDS_BEYOND_THE_PROBE if "
        "it is not a per-probe setting"
    )


def test_the_store_wins_over_the_yaml_seed(configured) -> None:
    yaml_path, store_path = configured
    targets = main.load_targets(yaml_path, store_path=store_path)
    assert [t.url for t in targets] == [URL], "the YAML seed must not be read"


@pytest.mark.parametrize(
    "field", sorted(set(FIELD_TO_KWARG) | set(FIELDS_BEYOND_THE_PROBE)))
def test_each_stored_field_survives_the_load(field, configured) -> None:
    """Parametrised over the schema, not hand-listed. This is the assertion
    that would have failed the day `maintenance` was dropped."""
    yaml_path, store_path = configured
    target = main.load_targets(yaml_path, store_path=store_path)[0]
    assert getattr(target, field) == FULL_ENTRY[field], (
        f"'{field}' was configured but did not reach the Target object"
    )


# =============================================================================
# B. The Target reaches the probe
# =============================================================================


@pytest.mark.parametrize("field,kwarg", sorted(FIELD_TO_KWARG.items()))
async def test_each_field_reaches_check_url(field, kwarg, configured, monkeypatch,
                                            tmp_path) -> None:
    """The far end of the seam.

    Surviving `load_targets` is half the journey; `run_health_checks` still has
    to hand it over. Capturing the keyword arguments check_url really receives
    is the only place where "configured" and "in effect" are the same claim.
    """
    yaml_path, store_path = configured
    seen: dict = {}

    async def _capture(url, session, state, **kwargs):
        seen.update(kwargs)
        seen["url"] = url
        await state.set_up(url)
        return main.CheckOutcome(url=url, status="UP",
                                 checked_at="2026-01-01T00:00:00Z", phases=None)

    monkeypatch.setattr(main, "check_url", _capture)
    await main.run_health_checks(
        targets=main.load_targets(yaml_path, store_path=store_path),
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.db",
    )

    assert seen, "check_url was never called"
    assert seen[kwarg] == FULL_ENTRY[field], (
        f"'{field}' reached the Target but not check_url — configured and "
        "in effect are different claims, and only the second one matters"
    )


# =============================================================================
# C. The whole cycle, against files rather than objects
# =============================================================================


async def test_a_full_cycle_reads_and_writes_the_real_files(configured, monkeypatch,
                                                            tmp_path) -> None:
    """Config in a file, state out to a file, nothing constructed by hand.

    Every other suite in this project starts from an object it built. This one
    starts where the deployment starts — a JSON file the dashboard wrote — and
    ends where an operator looks.
    """
    yaml_path, store_path = configured

    async def _probe(url, session, state, **kwargs):
        await state.set_up(url)
        return main.CheckOutcome(url=url, status="UP",
                                 checked_at="2026-01-01T00:00:00Z", phases=None)

    monkeypatch.setattr(main, "check_url", _probe)
    state_path = tmp_path / "state.json"
    down = await main.run_health_checks(
        targets=main.load_targets(yaml_path, store_path=store_path),
        state_path=state_path,
        history_path=tmp_path / "history.db",
    )

    assert down == 0
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert URL in stored["targets"]
    assert stored["schema_version"] == main.STATE_SCHEMA_VERSION


async def test_a_target_removed_from_the_store_leaves_the_state(configured,
                                                                monkeypatch,
                                                                tmp_path) -> None:
    """Pruning, exercised through the same seam. The dashboard's delete button
    writes the store; nothing else tells the probe that target is gone."""
    yaml_path, store_path = configured
    state_path = tmp_path / "state.json"

    async def _probe(url, session, state, **kwargs):
        await state.set_up(url)
        return main.CheckOutcome(url=url, status="UP",
                                 checked_at="2026-01-01T00:00:00Z", phases=None)

    monkeypatch.setattr(main, "check_url", _probe)
    kwargs = dict(state_path=state_path, history_path=tmp_path / "history.db")

    await main.run_health_checks(
        targets=main.load_targets(yaml_path, store_path=store_path), **kwargs)
    assert URL in json.loads(state_path.read_text(encoding="utf-8"))["targets"]

    other = dict(FULL_ENTRY, url="https://other.example")
    targetstore.write_store([targetstore.validate_entry(other)], store_path)
    await main.run_health_checks(
        targets=main.load_targets(yaml_path, store_path=store_path), **kwargs)

    remaining = json.loads(state_path.read_text(encoding="utf-8"))["targets"]
    assert URL not in remaining, "a target deleted from the store leaves the state"
    assert "https://other.example" in remaining


# =============================================================================
# C. The WRITE path — the seam this file was missing
# =============================================================================
#
# Everything above follows a field from the store inward. Nothing followed the
# other direction, and that is where a real hole opened: the dashboard editor
# builds its PUT from the inputs it rendered, and the API replaces the stored
# list with exactly that. Any accepted field the editor does not render is
# therefore DELETED by a save -- silently, with a success message.
#
# Live today, and it destroys measured work: a save wipes cloudflare's
# degraded_backend_ms (2000, calibrated over 920 probes, back to the global 500
# that fired on 63.8% of them) and httpbin's trend_alerts: false (back to firing
# on 63.3% of evaluations).
#
# This guard does not fix that. It stops it from growing: a NEW accepted field
# now has to be rendered by the editor or listed here with a reason.

#: Accepted by the store, not rendered as an input, but carried through a save
#: untouched (the EKEEP list in dashboard.py). Not editable from the UI, and
#: not destroyed by it either — which is the property that matters.
EDITOR_PRESERVED: dict[str, str] = {
    "expect_substring":
        "can hold a ${VAR} secret reference; not worth an input, and a save "
        "must not turn a body-checked target into a status-code-only one",
    "maintenance":
        "a list of objects, so editing it needs a repeating sub-form; a save "
        "must not un-silence planned work",
}

#: Accepted by the store, neither rendered nor preserved — so DELETED by a save.
#: Empty, and it has to stay empty. Every entry here would name real damage.
EDITOR_OMITS: dict[str, str] = {}


def test_the_yaml_path_keeps_remind_trend_and_maintenance(tmp_path) -> None:
    """The other branch of load_targets, which nothing here covered.

    Everything above follows a field from the STORE. But load_targets has a
    second path -- the one every fresh install takes, because data/targets.json
    does not exist until someone opens the dashboard -- and it built Target
    without remind, trend_alerts or maintenance. The values landed on the
    dataclass defaults with no error and no warning, so an operator who
    configured a maintenance window in the file the README tells them to edit
    got paged at 2 AM inside it.

    Same shape as the per-target backend threshold that shipped inert, in the
    branch nobody asserted.
    """
    yaml_path = tmp_path / "targets.yaml"
    yaml_path.write_text(
        "targets:\n"
        "  - url: https://yaml-only.example\n"
        "    remind: true\n"
        "    trend_alerts: false\n"
        "    maintenance:\n"
        "      - days: [0, 6]\n"
        "        start: '02:00'\n"
        "        end: '04:00'\n",
        encoding="utf-8",
    )

    target = main.load_targets(yaml_path, store_path=tmp_path / "no-store.json")[0]

    assert target.remind is True, "remind was configured and did not arrive"
    assert target.trend_alerts is False, "trend_alerts was configured and did not arrive"
    assert target.maintenance == [
        {"days": [0, 6], "start": "02:00", "end": "04:00"}
    ], "the maintenance window was configured and did not arrive"


def test_the_yaml_default_is_resolved_at_call_time(tmp_path, monkeypatch) -> None:
    """`path: Path = TARGETS_FILE` evaluated the default once, at import.

    So a later change to TARGETS_FILE never reached load_targets. Not live —
    every call site passes the path — but it is the same shape that already
    caused a real bug in targetstore, where the fix was the same one: resolve
    the path when the function runs, not when the module loads.
    """
    moved = tmp_path / "elsewhere.yaml"
    moved.write_text("targets:\n  - https://moved.example\n", encoding="utf-8")
    monkeypatch.setattr(main, "TARGETS_FILE", moved)

    targets = main.load_targets(store_path=tmp_path / "no-store.json")
    assert [t.url for t in targets] == ["https://moved.example"], (
        "load_targets read the path bound at import, not the current one"
    )


def test_a_bad_yaml_only_field_is_refused_by_the_same_rules_as_the_store(tmp_path) -> None:
    """One set of rules, not two. Two is how these fields drifted apart."""
    yaml_path = tmp_path / "targets.yaml"
    yaml_path.write_text(
        "targets:\n"
        "  - url: https://yaml-only.example\n"
        "    trend_alerts: 'false'\n",     # string, not bool — the classic YAML trap
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        main.load_targets(yaml_path, store_path=tmp_path / "no-store.json")


def test_every_store_field_is_either_editable_or_a_known_omission() -> None:
    """A field the store accepts and the editor drops is a field a save deletes.

    Read from dashboard.py's source rather than from a list kept beside it,
    because a list beside it is the thing that drifts. The workflow test in
    tests/test_grafana_dashboards.py already reads YAML this way for the same
    reason.
    """
    import re

    source   = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(
        encoding="utf-8")
    # Anchored to the EF constant, not to any ['a','b'] pair in the file: the
    # loose pattern also matched an unrelated array and reported a preserved
    # field as rendered.
    rendered = set(re.findall(r"dataset\.f='([a-z_]+)'", source))
    _ef = re.search(r"const EF=\[(.*?)\];", source, re.S)
    if _ef:
        rendered |= set(re.findall(r"\['([a-z_]+)',", _ef.group(1)))

    assert "url" in rendered, (
        "the scan found no fields at all — the editor's markup changed shape "
        "and this test is now checking nothing"
    )

    preserved = set(re.findall(r"const EKEEP=\[([^\]]*)\]", source))
    kept = {f.strip().strip("'\"") for group in preserved
            for f in group.split(",") if f.strip()}

    unaccounted = targetstore.ALLOWED_KEYS - rendered - kept - set(EDITOR_OMITS)
    assert not unaccounted, (
        f"the store accepts {sorted(unaccounted)} and the dashboard editor "
        "neither renders nor preserves it, so saving from the dashboard will "
        "delete it. Add an input, add it to EKEEP in dashboard.py, or add it to "
        "EDITOR_OMITS with the damage it does."
    )


def test_the_preserved_fields_are_actually_carried_through() -> None:
    """A field listed as preserved has to be preserved by the code, not by this
    file's optimism.

    EDITOR_PRESERVED is a claim about dashboard.py. If EKEEP and this list drift
    apart, the claim becomes the same kind of comforting documentation that let
    a save delete four fields while a test said everything was accounted for.
    """
    import re

    source = (Path(__file__).resolve().parents[1] / "dashboard.py").read_text(
        encoding="utf-8")
    groups = re.findall(r"const EKEEP=\[([^\]]*)\]", source)
    assert groups, "EKEEP is gone from dashboard.py — nothing is preserved now"
    kept = {f.strip().strip("'\"") for g in groups
            for f in g.split(",") if f.strip()}

    assert kept == set(EDITOR_PRESERVED), (
        f"dashboard.py preserves {sorted(kept)} but this file documents "
        f"{sorted(EDITOR_PRESERVED)}"
    )
    # And preserving is a fallback, not a hiding place: nothing preserved should
    # also be rendered, or the two paths would fight over the same key.
    # Anchored to the EF constant, not to any ['a','b'] pair in the file: the
    # loose pattern also matched an unrelated array and reported a preserved
    # field as rendered.
    rendered = set(re.findall(r"dataset\.f='([a-z_]+)'", source))
    _ef = re.search(r"const EF=\[(.*?)\];", source, re.S)
    if _ef:
        rendered |= set(re.findall(r"\['([a-z_]+)',", _ef.group(1)))
    assert not (kept & rendered), sorted(kept & rendered)


def test_no_accepted_field_is_silently_deleted_by_a_save() -> None:
    """The headline property, stated on its own so it cannot be diluted.

    EDITOR_OMITS is empty and must stay empty. An entry there is a field the
    store accepts and a save destroys — silently, with a success message.
    """
    assert EDITOR_OMITS == {}, (
        f"a save from the dashboard deletes {sorted(EDITOR_OMITS)}"
    )
