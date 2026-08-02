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
    "remind"          : True,
    "maintenance"     : [{"days": [0, 6], "start": "02:00", "end": "04:00"}],
}

#: Store key -> the check_url keyword it must arrive as. `url` travels
#: positionally; `expect_substring` is resolved on the way through (a literal
#: passes unchanged) so it is checked separately.
FIELD_TO_KWARG: dict[str, str] = {
    "expected_status" : "expected_status",
    "timeout_s"       : "timeout_s",
    "degraded_ttfb_ms": "degraded_ttfb_ms",
    "degraded_rtt_ms" : "degraded_rtt_ms",
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
    accounted = set(FIELD_TO_KWARG) | {"url", "expect_substring"}
    missing   = targetstore.ALLOWED_KEYS - accounted
    assert not missing, (
        f"store accepts {sorted(missing)} but this test does not follow it "
        "through to check_url — add it to FIELD_TO_KWARG"
    )


def test_the_store_wins_over_the_yaml_seed(configured) -> None:
    yaml_path, store_path = configured
    targets = main.load_targets(yaml_path, store_path=store_path)
    assert [t.url for t in targets] == [URL], "the YAML seed must not be read"


@pytest.mark.parametrize("field", sorted(FIELD_TO_KWARG))
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
