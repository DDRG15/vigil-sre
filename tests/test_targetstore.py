"""
tests/test_targetstore.py — the target list when the dashboard owns it.

What is actually being defended
-------------------------------
This is the first write path in the project. Every entry here arrives over
HTTP and becomes a URL the monitor will fetch from its own network, on a
schedule, forever. The interesting attack is not vandalising a list: it is
getting a probe inside the perimeter with the results rendered on a page.

So the tests that matter are the refusals — scheme, ranges, unknown keys — and
the two states that must never be confused: "no store yet, use the YAML" and
"the operator deleted every target".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import targetstore
from targetstore import (
    MAX_TARGETS,
    ValidationError,
    read_store,
    validate_all,
    validate_entry,
    write_store,
)

URL = "https://example.com/health"


# =============================================================================
# A. What the store refuses
# =============================================================================


@pytest.mark.parametrize("scheme", ["file", "gopher", "ftp", "javascript", ""])
def test_only_http_and_https_are_accepted(scheme) -> None:
    """A scheme other than http(s) turns "monitor this endpoint" into "read
    this path", using the monitor's own privileges. The probe is the
    capability being handed out here, not the list entry."""
    url = f"{scheme}://etc/passwd" if scheme else "etc/passwd"
    with pytest.raises(ValidationError, match="[Ee]squema"):
        validate_entry({"url": url})


def test_an_unknown_field_is_rejected_not_ignored() -> None:
    """A silently dropped field is a setting the operator believes is in
    effect. Refusing is the only answer that keeps the page honest."""
    with pytest.raises(ValidationError, match="no reconocido"):
        validate_entry({"url": URL, "degraded_rtt": 400})


@pytest.mark.parametrize("field,value", [
    ("expected_status",  99),
    ("expected_status",  600),
    ("timeout_s",        0),
    ("timeout_s",        301),
    ("degraded_rtt_ms",  0),
    ("degraded_ttfb_ms", 0),
])
def test_numeric_fields_are_range_checked(field, value) -> None:
    """The same bounds load_targets enforces for the YAML path. An API that
    accepted what the file rejects would be a way AROUND the validation, not a
    second door to it -- timeout_s=0 disables the timeout entirely in aiohttp,
    which freezes the whole run."""
    with pytest.raises(ValidationError):
        validate_entry({"url": URL, field: value})


@pytest.mark.parametrize("bad", [None, "", "   ", 42, {"no": "url"}, []])
def test_an_entry_without_a_usable_url_is_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        validate_entry(bad if isinstance(bad, (dict, list)) else {"url": bad})


def test_a_boolean_is_not_a_number() -> None:
    """`True` is an int in Python. Letting it through would store
    expected_status=1, which no server returns."""
    with pytest.raises(ValidationError, match="número"):
        validate_entry({"url": URL, "expected_status": True})


def test_remind_must_be_a_boolean() -> None:
    with pytest.raises(ValidationError, match="true o false"):
        validate_entry({"url": URL, "remind": "yes"})


def test_duplicate_urls_are_rejected() -> None:
    """Two rows for one URL would each write history under the same key and
    each render a row, so the page would disagree with itself."""
    with pytest.raises(ValidationError, match="[Dd]uplicado"):
        validate_all([{"url": URL}, {"url": URL}])


def test_the_list_has_a_ceiling() -> None:
    with pytest.raises(ValidationError):
        validate_all([{"url": f"https://x{i}.com"} for i in range(MAX_TARGETS + 1)])


# =============================================================================
# B. What it accepts, and how it normalises
# =============================================================================


def test_a_full_entry_round_trips() -> None:
    entry = validate_entry({
        "url": f"  {URL}  ", "expected_status": 204, "timeout_s": 10,
        "degraded_ttfb_ms": 3000, "degraded_rtt_ms": 250, "remind": True,
        "expect_substring": '"ok"',
    })
    assert entry["url"] == URL, "surrounding whitespace is stripped"
    assert entry["expected_status"] == 204 and isinstance(entry["expected_status"], int)
    assert entry["timeout_s"] == 10.0 and isinstance(entry["timeout_s"], float)
    assert entry["remind"] is True


def test_remind_defaults_to_off() -> None:
    """Opt-in, deliberately: a target added to watch something break would
    otherwise nag forever, which is the alert fatigue this project spent
    several rounds removing."""
    assert validate_entry({"url": URL})["remind"] is False


def test_omitted_overrides_stay_omitted() -> None:
    """None means "use the module default". Writing an explicit null would
    make every future default change invisible to targets created today."""
    entry = validate_entry({"url": URL})
    assert "timeout_s" not in entry
    assert "degraded_rtt_ms" not in entry


# =============================================================================
# C. Persistence
# =============================================================================


def test_no_store_is_not_an_empty_store(tmp_path: Path) -> None:
    """The distinction the whole fallback rests on. None means "never written,
    use targets.yaml"; [] means "the operator deleted every target".
    Collapsing them would resurrect a deleted list on the next run."""
    store = tmp_path / "targets.json"
    assert read_store(store) is None

    write_store([], store)
    assert read_store(store) == []


def test_a_write_survives_a_read(tmp_path: Path) -> None:
    store = tmp_path / "targets.json"
    write_store([{"url": URL, "remind": True}], store)
    assert read_store(store) == [{"url": URL, "remind": True}]


def test_the_directory_is_created_if_absent(tmp_path: Path) -> None:
    store = tmp_path / "nested" / "deeper" / "targets.json"
    write_store([{"url": URL}], store)
    assert store.exists()


def test_an_invalid_write_leaves_the_previous_list_intact(tmp_path: Path) -> None:
    """Validation runs BEFORE the file is touched. A rejected save that had
    already truncated the store would lose a working configuration to a typo."""
    store = tmp_path / "targets.json"
    write_store([{"url": URL}], store)
    with pytest.raises(ValidationError):
        write_store([{"url": "file:///etc/passwd"}], store)
    assert read_store(store) == [{"url": URL, "remind": False}]


def test_a_corrupt_store_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    """Reading happens inside the probe, every run. A malformed file must
    degrade to "use the YAML", not take monitoring down with it."""
    store = tmp_path / "targets.json"
    store.write_text("{not json", encoding="utf-8")
    assert read_store(store) is None


def test_a_store_holding_an_invalid_entry_falls_back(tmp_path: Path) -> None:
    """Hand-edited, or written by an older version. Validating on read as well
    as on write means the probe never acts on an entry the API would refuse."""
    store = tmp_path / "targets.json"
    store.write_text(json.dumps({"targets": [{"url": "file:///etc/passwd"}]}),
                     encoding="utf-8")
    assert read_store(store) is None


# =============================================================================
# D. The HTTP surface — the part an attacker can actually reach
# =============================================================================

import json as _json
import threading
import urllib.error
import urllib.request

from api import DEFAULT_HOST, serve

TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def live(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("API_WRITE_TOKEN", TOKEN)
    monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "targets.json")
    server = serve(DEFAULT_HOST, 0, state_path=tmp_path / "state.json",
                   history_path=tmp_path / "history.db")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{DEFAULT_HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _put(base: str, body: dict, token: str | None = TOKEN):
    req = urllib.request.Request(
        base + "/api/targets", method="PUT",
        data=_json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"X-Vigil-Token": token} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json.loads(exc.read())


def test_a_write_without_the_token_is_refused(live) -> None:
    """The token is what separates the operator from anything else that can
    reach the port. Adding a target is not editing a row -- it makes this
    monitor fetch that URL from inside the network, on a schedule."""
    status, body = _put(live, {"targets": [{"url": URL}]}, token=None)
    assert status == 401
    assert read_store(targetstore.STORE_FILE) is None, "nothing was written"


def test_a_write_with_the_wrong_token_is_refused(live) -> None:
    status, _ = _put(live, {"targets": [{"url": URL}]}, token="wrong")
    assert status == 401


def test_writes_fail_closed_when_no_token_is_configured(live, monkeypatch) -> None:
    """Without a configured token there is no way to tell an operator from
    anyone else, so the honest answer is to refuse rather than accept
    everything. Reads are unaffected."""
    monkeypatch.delenv("API_WRITE_TOKEN", raising=False)
    status, _ = _put(live, {"targets": [{"url": URL}]})
    assert status == 401


def test_a_valid_write_is_stored_and_returned(live) -> None:
    status, body = _put(live, {"targets": [{"url": URL, "remind": True}]})
    assert status == 200
    assert body["targets"] == [{"url": URL, "remind": True}]
    assert read_store(targetstore.STORE_FILE) == [{"url": URL, "remind": True}]


def test_an_invalid_write_answers_with_the_reason(live) -> None:
    """400 with the message, not a traceback: it is rendered next to the field
    the operator just typed."""
    status, body = _put(live, {"targets": [{"url": "file:///etc/passwd"}]})
    assert status == 400
    assert "esquema" in body["error"].lower()


def test_reading_the_target_list_needs_no_token(live) -> None:
    """Reads were already open before this feature and stay that way. The
    response says which file is authoritative, because two files that both
    look canonical is worse than either alone."""
    with urllib.request.urlopen(live + "/api/targets", timeout=10) as resp:
        body = _json.loads(resp.read())
    assert body["targets"] == []
    assert body["managed"] is False, "nothing written yet -- targets.yaml rules"
    assert body["writable"] is True

    _put(live, {"targets": [{"url": URL}]})
    with urllib.request.urlopen(live + "/api/targets", timeout=10) as resp:
        assert _json.loads(resp.read())["managed"] is True
