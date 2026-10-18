"""
tests/test_store_integrity.py — a store that exists but cannot be used is an
error, never a reason to probe the seed.

The store (data/targets.json) is the live target list once the dashboard has
saved. read_store used to answer None both for "no file" and for "a file I could
not use", and None means "run the YAML". So a store broken by a hand edit, a
half-restored backup or an unset ${VAR} made the monitor probe a stale seed:
targets added from the dashboard stopped being probed, deleted ones came back,
the round exited 0 with a green heartbeat, and the pruner deleted state for
every target that was not in the seed.

These tests pin the distinction: None is reserved for "no file", [] is a valid
store with nothing in it, and everything else that is not a usable list raises.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api
import main
import targetctl
import targetstore

TOKEN = "test-token-not-a-real-secret"

SEED_YAML = "targets:\n  - https://seed-only.example\n"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """(seed, store): both redirected into tmp_path, the seed already valid."""
    seed  = tmp_path / "targets.yaml"
    store = tmp_path / "data" / "targets.json"
    seed.write_text(SEED_YAML, encoding="utf-8")
    monkeypatch.setattr(targetstore, "SEED_FILE", seed)
    monkeypatch.setattr(targetstore, "STORE_FILE", store)
    monkeypatch.delenv("STORE_TEST_TOKEN", raising=False)
    return seed, store


def _write(store: Path, text: str) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(text, encoding="utf-8")


#: Every way a store can be present and unusable, as (id, how to produce it).
#: Each takes the store path and leaves it broken.
def _bad_json(store: Path) -> None:
    _write(store, "{not json")


def _bad_entry(store: Path) -> None:
    _write(store, json.dumps({"targets": [{"url": "file:///etc/passwd"}]}))


def _not_a_list(store: Path) -> None:
    _write(store, json.dumps({"targets": "nope"}))


def _no_targets_key(store: Path) -> None:
    _write(store, json.dumps({"something": "else"}))


def _not_utf8(store: Path) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"\xff\xfe{\"targets\": []}")


def _directory_in_its_place(store: Path) -> None:
    store.mkdir(parents=True)


def _duplicate(store: Path) -> None:
    _write(store, json.dumps({"targets": [
        {"url": "https://a.example"}, {"url": "https://a.example"}]}))


BREAKAGES = [
    pytest.param(_bad_json, id="invalid-json"),
    pytest.param(_bad_entry, id="invalid-entry"),
    pytest.param(_not_a_list, id="targets-not-a-list"),
    pytest.param(_no_targets_key, id="no-targets-key"),
    pytest.param(_not_utf8, id="not-utf8"),
    pytest.param(_directory_in_its_place, id="directory-in-its-place"),
    pytest.param(_duplicate, id="duplicate-urls"),
]


# =============================================================================
# A. read_store: None means "no file" and nothing else
# =============================================================================


def test_no_file_is_none(paths) -> None:
    _, store = paths
    assert targetstore.read_store(store) is None


def test_a_valid_empty_store_is_an_empty_list_not_none(paths) -> None:
    _, store = paths
    _write(store, json.dumps({"targets": []}))
    assert targetstore.read_store(store) == []


@pytest.mark.parametrize("break_it", BREAKAGES)
def test_an_unusable_store_raises_store_error(paths, break_it) -> None:
    _, store = paths
    break_it(store)

    with pytest.raises(targetstore.StoreError) as exc:
        targetstore.read_store(store)

    assert "\n" not in str(exc.value), "the reason is one line"
    assert str(store) in str(exc.value), "and it names the file"


def test_an_unreadable_file_raises_store_error(paths, monkeypatch) -> None:
    """Permission denied cannot be produced portably with chmod (Windows ignores
    it), so the read itself is made to fail the way the OS would."""
    _, store = paths
    _write(store, json.dumps({"targets": [{"url": "https://a.example"}]}))
    real = Path.read_text

    def deny(self, *args, **kwargs):
        if self == store:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)

    with pytest.raises(targetstore.StoreError, match="Permission denied"):
        targetstore.read_store(store)


def test_an_unset_variable_is_not_a_structural_error(paths, monkeypatch) -> None:
    """Showing the list must not depend on which variables THIS process holds."""
    _, store = paths
    monkeypatch.delenv("STORE_TEST_TOKEN", raising=False)
    _write(store, json.dumps({"targets": [
        {"url": "https://a.example", "expect_substring": "${STORE_TEST_TOKEN}"}]}))

    got = targetstore.read_store(store)

    assert got is not None and got[0]["expect_substring"] == "${STORE_TEST_TOKEN}"


def test_check_env_names_the_missing_variable(paths, monkeypatch) -> None:
    _, store = paths
    monkeypatch.delenv("STORE_TEST_TOKEN", raising=False)
    _write(store, json.dumps({"targets": [
        {"url": "https://a.example", "expect_substring": "${STORE_TEST_TOKEN}"}]}))

    with pytest.raises(targetstore.StoreError, match="STORE_TEST_TOKEN"):
        targetstore.read_store(store, check_env=True)


# =============================================================================
# B. The monitor fails the round and never falls back to the seed
# =============================================================================


@pytest.mark.parametrize("break_it", BREAKAGES)
def test_a_broken_store_fails_the_round_instead_of_probing_the_seed(
        paths, break_it, caplog) -> None:
    seed, store = paths
    break_it(store)

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as exit_info:
            main.load_targets(seed, store_path=store)

    assert exit_info.value.code == 1
    critical = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "a failed round has to say why at CRITICAL"
    assert "seed-only.example" not in " ".join(critical)


def test_an_unset_variable_in_the_store_fails_the_round_naming_it(
        paths, monkeypatch, caplog) -> None:
    """Reproduced: unset HEALTH_TOKEN made read_store return None and the
    monitor logged 'falling back to targets.yaml'."""
    seed, store = paths
    monkeypatch.delenv("STORE_TEST_TOKEN", raising=False)
    _write(store, json.dumps({"targets": [
        {"url": "https://stored.example", "expect_substring": "${STORE_TEST_TOKEN}"}]}))

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as exit_info:
            main.load_targets(seed, store_path=store)

    assert exit_info.value.code == 1
    assert any("STORE_TEST_TOKEN" in r.getMessage() for r in caplog.records)


def test_a_valid_empty_store_does_not_resurrect_the_seed(paths) -> None:
    seed, store = paths
    _write(store, json.dumps({"targets": []}))

    assert main.load_targets(seed, store_path=store) == []


def test_no_store_still_runs_the_seed(paths) -> None:
    seed, store = paths
    assert [t.url for t in main.load_targets(seed, store_path=store)] == [
        "https://seed-only.example"]


# =============================================================================
# C. The API reports the error and refuses writes built on a failed read
# =============================================================================


@pytest.fixture
def server(paths, tmp_path: Path, monkeypatch):
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
    request = urllib.request.Request(
        base + "/api/targets", method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Vigil-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.mark.parametrize("break_it", BREAKAGES)
def test_get_surfaces_a_store_error_instead_of_the_seed(server, paths, break_it) -> None:
    _, store = paths
    break_it(store)

    status, body = _call(server)

    assert status == 500
    assert body["store_error"] and str(store) in body["store_error"]
    assert "targets" not in body, "neither an empty list nor the seed"
    assert "seed-only.example" not in json.dumps(body)


@pytest.mark.parametrize("break_it", BREAKAGES)
def test_put_is_refused_while_the_store_is_unreadable(server, paths, break_it) -> None:
    _, store = paths
    break_it(store)
    before = store.read_bytes() if store.is_file() else None

    status, body = _call(server, "PUT", {"targets": [{"url": "https://new.example"}]})

    assert status == 409
    assert body["store_error"]
    assert "delete" in body["error"].lower(), "the way out is in the message"
    if before is not None:
        assert store.read_bytes() == before, "the broken file was overwritten"
    else:
        assert store.is_dir(), "the directory in its place was touched"


def test_recovery_is_deleting_the_file_then_saving(server, paths) -> None:
    _, store = paths
    _bad_json(store)
    assert _call(server, "PUT", {"targets": [{"url": "https://new.example"}]})[0] == 409

    store.unlink()

    status, body = _call(server)
    assert status == 200 and body["managed"] is False
    assert [e["url"] for e in body["targets"]] == ["https://seed-only.example"]
    assert _call(server, "PUT", {"targets": [{"url": "https://new.example"}]})[0] == 200


def test_get_keeps_no_store_and_an_empty_store_apart(server, paths) -> None:
    _, store = paths

    status, body = _call(server)
    assert (status, body["managed"]) == (200, False)
    assert [e["url"] for e in body["targets"]] == ["https://seed-only.example"]

    _write(store, json.dumps({"targets": []}))
    status, body = _call(server)
    assert (status, body["managed"], body["targets"]) == (200, True, [])


def test_get_shows_a_reference_the_api_cannot_resolve(server, paths, monkeypatch) -> None:
    _, store = paths
    monkeypatch.delenv("STORE_TEST_TOKEN", raising=False)
    _write(store, json.dumps({"targets": [
        {"url": "https://a.example", "expect_substring": "${STORE_TEST_TOKEN}"}]}))

    status, body = _call(server)

    assert status == 200 and body["managed"] is True
    assert body["targets"][0]["expect_substring"] == "${STORE_TEST_TOKEN}"


def test_the_pages_survive_a_broken_store(server, paths) -> None:
    """/ and /partial/targets read the store for the maintenance badges; an
    exception there kills the handler thread and the page never answers."""
    _, store = paths
    _bad_json(store)

    for route in ("/", "/partial/targets"):
        with urllib.request.urlopen(server + route, timeout=30) as response:
            assert response.status == 200


# =============================================================================
# D. targetctl surfaces the error and writes nothing
# =============================================================================


@pytest.fixture
def ctl_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIGIL_API", raising=False)
    monkeypatch.setenv("API_WRITE_TOKEN", TOKEN)


@pytest.mark.parametrize("argv", [
    ["list"],
    ["add", "https://new.example"],
    ["remove", "https://a.example"],
])
def test_targetctl_reports_a_store_error_and_writes_nothing(
        server, paths, ctl_env, capsys, argv) -> None:
    _, store = paths
    _bad_json(store)
    before = store.read_bytes()

    code = targetctl.main(["--api", server, *argv])
    err = capsys.readouterr().err

    assert code == 1
    assert "store" in err.lower() and "not valid JSON" in err
    assert "delete" in err.lower(), "the recovery step is on the screen"
    assert store.read_bytes() == before
