"""
tests/test_editor_seed.py — the editor must open on what the monitor is running.

What is actually being defended
-------------------------------
Before the first save there is no data/targets.json, so the monitor runs on
targets.yaml. The editor used to paint the store's answer for that state,
which is an empty list, and "Save" sends the whole list. An operator who
opened the editor on a fresh install and clicked save without touching a row
replaced four monitored targets with zero -- the monitor then loaded 0 targets
and kept sending a healthy heartbeat, because nothing it was asked to check
had failed.

Four properties close that, and each test below names the one it holds:

  1. GET shows the YAML while there is no store, as the store would hold it
     and WITHOUT resolving ${VAR}: a resolved secret must not leave through
     an API that needs no token to read.
  2. Saving what the editor showed loses nothing, field by field.
  3. Emptying the list takes an explicit mark, so it can still be done and
     can no longer be done by accident.
  4. The store is consulted before the YAML, so a broken seed cannot kill a
     round that has a perfectly good store.

The server fixtures point BOTH the store and the seed at tmp_path. The seed
path is ambient state exactly as the store path is: with the real
targets.yaml beside the process, a test's answer would depend on the cwd.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import dashboard
import main
import targetstore

TOKEN  = "test-token-not-a-real-secret"
SECRET = "s3cr3t-body-marker-9f2c41"

#: Every field the store accepts, written the way an operator writes them in the
#: file the README points at. `expect_substring` is a ${VAR} reference, which
#: is the field a careless GET would resolve.
FULL_YAML = """\
targets:
  - https://plain.example
  - url: https://full.example/health
    expect_substring: ${HEALTH_TOKEN}
    expected_status: 204
    timeout_s: 10
    degraded_ttfb_ms: 3000
    degraded_rtt_ms: 250
    degraded_backend_ms: 2000
    remind: true
    trend_alerts: false
    maintenance:
      - days: [0, 6]
        start: '02:00'
        end: '04:00'
  - url: https://literal.example
    expect_substring: '"status":"ok"'
"""


@pytest.fixture
def seed(tmp_path: Path, monkeypatch) -> Path:
    """The seed path, redirected. Returned so a test can write the YAML."""
    path = tmp_path / "targets.yaml"
    monkeypatch.setattr(targetstore, "SEED_FILE", path)
    monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "data" / "targets.json")
    monkeypatch.setenv("HEALTH_TOKEN", SECRET)
    return path


@pytest.fixture
def server(seed: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("API_WRITE_TOKEN", TOKEN)
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


def _call(base: str, method: str = "GET", body: dict | None = None):
    """Return (status, parsed body, raw text). Errors are answers, not raises."""
    req = urllib.request.Request(
        base + "/api/targets", method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Vigil-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw), raw


def _load(seed: Path) -> list[main.Target]:
    return main.load_targets(seed, store_path=targetstore.STORE_FILE)


# =============================================================================
# A. GET shows the seed while there is no store
# =============================================================================


def test_get_without_a_store_shows_the_yaml_targets(server, seed) -> None:
    """An empty list here is what made the first save destructive."""
    seed.write_text(FULL_YAML, encoding="utf-8")
    assert not targetstore.STORE_FILE.exists()

    status, body, _ = _call(server)

    assert status == 200
    assert body["managed"] is False
    assert [e["url"] for e in body["targets"]] == [t.url for t in _load(seed)]
    assert len(body["targets"]) == 3
    assert body["seed_error"] is None


def test_get_never_returns_the_resolved_secret(server, seed) -> None:
    """The read is open -- no token -- so a resolved ${VAR} here is a secret
    published to anything that can reach the port."""
    seed.write_text(FULL_YAML, encoding="utf-8")

    _, body, raw = _call(server)

    assert SECRET not in raw
    full = next(e for e in body["targets"] if e["url"] == "https://full.example/health")
    assert full["expect_substring"] == "${HEALTH_TOKEN}"


def test_get_shows_a_reference_the_api_process_cannot_resolve(server, seed, monkeypatch) -> None:
    """Showing is not resolving. If the dashboard process lacks the variable the
    list must still render; the PUT is what refuses, with its own message."""
    monkeypatch.delenv("HEALTH_TOKEN", raising=False)
    seed.write_text(FULL_YAML, encoding="utf-8")

    _, body, _ = _call(server)

    assert len(body["targets"]) == 3
    assert body["seed_error"] is None


def test_get_drops_unknown_yaml_keys_exactly_as_the_monitor_does(server, seed) -> None:
    """load_targets ignores a key it does not know. The store rejects one, so
    the seed read has to drop it, or a YAML the monitor loads fine would be
    unshowable and the editor would open empty again."""
    seed.write_text(
        "targets:\n  - url: https://x.example\n    owner: platform-team\n",
        encoding="utf-8",
    )

    _, body, _ = _call(server)

    assert [e["url"] for e in body["targets"]] == ["https://x.example"]
    assert "owner" not in body["targets"][0]
    assert body["seed_error"] is None


@pytest.mark.parametrize("content,needle", [
    (None, "does not exist"),
    ("targets: [unclosed\n  - : :", "YAML"),
    ("targets:\n  - url: https://x.example\n    degraded_ttfb_ms: 0.5\n", "degraded_ttfb_ms"),
    ("targets:\n  - {not_a_url: 1}\n", "invalid entry"),
])
def test_an_unshowable_seed_is_reported_not_painted_as_empty(
        server, seed, content, needle) -> None:
    """An empty list with no explanation is indistinguishable from "nothing is
    monitored", which is the state this fix exists to stop producing."""
    if content is not None:
        seed.write_text(content, encoding="utf-8")

    status, body, _ = _call(server)

    assert status == 200
    assert body["managed"] is False
    assert body["targets"] == []
    assert needle in (body.get("seed_error") or ""), body


def test_a_managed_store_wins_over_the_seed_and_reports_no_seed_error(server, seed) -> None:
    seed.write_text(FULL_YAML, encoding="utf-8")
    targetstore.write_store([{"url": "https://stored.example"}])

    _, body, _ = _call(server)

    assert body["managed"] is True
    assert body["seed_error"] is None
    assert [e["url"] for e in body["targets"]] == ["https://stored.example"]


# =============================================================================
# B. Saving what the editor showed loses nothing
# =============================================================================


def test_an_untouched_open_and_save_keeps_every_yaml_target(server, seed) -> None:
    """The reproduction: GET what the editor paints, PUT exactly that."""
    seed.write_text(FULL_YAML, encoding="utf-8")

    _, shown, _ = _call(server)
    status, _, _ = _call(server, "PUT", {"targets": shown["targets"]})

    assert status == 200
    assert len(_load(seed)) == 3, "the monitor lost targets to an untouched save"


def test_the_round_trip_preserves_every_field_and_keeps_references_as_references(
        server, seed) -> None:
    """Field by field, against what the monitor loaded from the YAML before the
    editor was ever opened. The same fields the 15 September fix defended."""
    seed.write_text(FULL_YAML, encoding="utf-8")
    before = _load(seed)
    assert not targetstore.STORE_FILE.exists()

    _, shown, _ = _call(server)
    _call(server, "PUT", {"targets": shown["targets"]})
    after = _load(seed)

    assert targetstore.STORE_FILE.exists()
    assert len(after) == len(before)
    for was, now in zip(before, after):
        assert dataclasses.asdict(now) == dataclasses.asdict(was), was.url

    full = next(t for t in after if t.url == "https://full.example/health")
    assert full.expect_substring == SECRET, "the reference no longer resolves at load"
    assert full.expect_substring_display == "${HEALTH_TOKEN} (from env)"
    assert full.maintenance == [{"days": [0, 6], "start": "02:00", "end": "04:00"}]
    assert full.degraded_backend_ms == 2000.0
    assert full.trend_alerts is False
    assert full.remind is True

    on_disk = targetstore.STORE_FILE.read_text(encoding="utf-8")
    assert "${HEALTH_TOKEN}" in on_disk
    assert SECRET not in on_disk, "the store holds a resolved secret"


# =============================================================================
# C. Emptying the list takes an explicit mark
# =============================================================================


def test_an_empty_put_without_the_mark_is_refused(server, seed) -> None:
    seed.write_text(FULL_YAML, encoding="utf-8")

    status, body, _ = _call(server, "PUT", {"targets": []})

    assert 400 <= status < 500
    assert "allow_empty" in body["error"]
    assert not targetstore.STORE_FILE.exists(), "a refused save wrote anyway"
    assert len(_load(seed)) == 3


def test_an_empty_put_over_an_existing_store_is_refused_too(server, seed) -> None:
    targetstore.write_store([{"url": "https://stored.example"}])

    status, _, _ = _call(server, "PUT", {"targets": []})

    assert 400 <= status < 500
    assert [t["url"] for t in targetstore.read_store()] == ["https://stored.example"]


@pytest.mark.parametrize("mark", ["true", 1, "yes", None, False])
def test_only_a_real_true_counts_as_the_mark(server, seed, mark) -> None:
    status, _, _ = _call(server, "PUT", {"targets": [], "allow_empty": mark})

    assert 400 <= status < 500
    assert not targetstore.STORE_FILE.exists()


def test_an_empty_put_with_the_mark_is_saved_and_stays_visible(server, seed) -> None:
    """Deleting everything remains possible, and remains a recorded decision:
    the store holds [] and the monitor honours it instead of falling back."""
    seed.write_text(FULL_YAML, encoding="utf-8")

    status, body, _ = _call(server, "PUT", {"targets": [], "allow_empty": True})

    assert status == 200
    assert body["targets"] == []
    assert targetstore.read_store() == []
    assert _load(seed) == []
    _, shown, _ = _call(server)
    assert shown["managed"] is True
    assert shown["targets"] == []


def test_a_marked_empty_put_replaces_an_existing_store(server, seed) -> None:
    """The second half of "deleting everything stays possible": the store is
    already there, so this is a replace and not a first write."""
    targetstore.write_store([{"url": "https://stored.example"}])

    status, _, _ = _call(server, "PUT", {"targets": [], "allow_empty": True})

    assert status == 200
    assert targetstore.read_store() == []


def test_the_mark_does_not_change_a_non_empty_save(server, seed) -> None:
    status, body, _ = _call(
        server, "PUT", {"targets": [{"url": "https://a.example"}], "allow_empty": True})

    assert status == 200
    assert [t["url"] for t in body["targets"]] == ["https://a.example"]


def test_the_editor_confirms_before_it_sends_the_mark() -> None:
    """The dashboard tests in this repo read the shipped script rather than run
    a browser, so this reads it too. Two properties: the mark is only ever set
    inside the empty-list branch, and confirm() runs before the request that
    would carry it."""
    script = dashboard._EDITOR_SCRIPT
    save = script[script.index("async function esave()"):]
    save = save[:save.index("\ndocument.getElementById('eadd')")]

    assert "allow_empty" in save
    assert "confirm(" in save
    assert save.index("confirm(") < save.index("allow_empty") < save.index("fetch("), (
        "the mark must be set after the user confirmed, and before the request"
    )
    assert len(re.findall(r"allow_empty", save)) == 1, (
        "allow_empty is set in more than one place; it must only follow the "
        "empty-list confirmation"
    )
    assert "rows.length===0" in save.replace(" ", ""), (
        "the mark is not guarded by an empty-list check"
    )


def test_the_editor_warns_before_overwriting_a_seed_it_could_not_show() -> None:
    """If the dashboard process cannot read targets.yaml the editor opens empty,
    and a save would replace the monitor's whole list with whatever was typed.
    The script must carry the server's reason and ask first."""
    script = dashboard._EDITOR_SCRIPT

    assert "d.seed_error" in script, "eload ignores seed_error"
    save = script[script.index("async function esave()"):]
    save = save[:save.index("\ndocument.getElementById('eadd')")]
    assert re.search(r"eseed\s*&&", save), "esave does not gate on the seed state"


# =============================================================================
# D. The store is consulted before the YAML
# =============================================================================


def test_malformed_yaml_with_a_valid_store_does_not_kill_the_round(
        tmp_path: Path, caplog) -> None:
    bad = tmp_path / "targets.yaml"
    bad.write_text("targets: [unclosed\n  - : :", encoding="utf-8")
    store = tmp_path / "s.json"
    targetstore.write_store([{"url": "https://example.com"}], store)

    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        got = main.load_targets(bad, store)

    assert [t.url for t in got] == ["https://example.com"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ignor" in m.lower() and str(bad) in m for m in warned), warned


def test_missing_yaml_with_a_valid_store_does_not_kill_the_round(
        tmp_path: Path, caplog) -> None:
    store = tmp_path / "s.json"
    targetstore.write_store([{"url": "https://example.com"}], store)

    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        got = main.load_targets(tmp_path / "nope.yaml", store)

    assert [t.url for t in got] == ["https://example.com"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ignor" in m.lower() and "nope.yaml" in m for m in warned), warned


def test_a_healthy_yaml_beside_a_valid_store_stays_silent(tmp_path: Path, caplog) -> None:
    """The warning is for a seed that is broken, not for every seed: a WARNING a
    minute about a file that is fine would train the on-call to skip it."""
    good = tmp_path / "targets.yaml"
    good.write_text("targets:\n  - https://yaml.example\n", encoding="utf-8")
    store = tmp_path / "s.json"
    targetstore.write_store([{"url": "https://example.com"}], store)

    with caplog.at_level(logging.WARNING, logger="sre.health_checker"):
        got = main.load_targets(good, store)

    assert [t.url for t in got] == ["https://example.com"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("content", [None, "targets: [unclosed\n  - : :"])
def test_without_a_store_a_missing_or_malformed_yaml_still_exits_1(
        tmp_path: Path, content) -> None:
    """Unchanged on purpose: with no store the YAML is the only source, and a
    round with no source must fail loudly, not run on nothing."""
    path = tmp_path / "targets.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main.load_targets(path, tmp_path / "no-store.json")

    assert exc.value.code == 1


def test_a_corrupt_store_exits_1_even_without_a_seed(tmp_path: Path) -> None:
    """A corrupt store fails the round whether or not the seed is usable; the
    seed is never a substitute (see test_store_integrity.py)."""
    store = tmp_path / "s.json"
    store.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main.load_targets(tmp_path / "nope.yaml", store)

    assert exc.value.code == 1


# =============================================================================
# E. Both processes read the same file
# =============================================================================


def test_the_monitor_and_the_api_default_to_the_same_seed_file(tmp_path: Path) -> None:
    """Two constants that both say "targets.yaml" are two chances to disagree.
    Run in a fresh interpreter: the autouse fixtures patch the module-level
    values for every in-process test, which would hide a drift."""
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import main, targetstore; "
        "assert main.TARGETS_FILE == targetstore.SEED_FILE, "
        "(main.TARGETS_FILE, targetstore.SEED_FILE)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
        text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    assert done.returncode == 0, done.stderr
