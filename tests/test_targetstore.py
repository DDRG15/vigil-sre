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


# =============================================================================
# E. The write path and the reader must agree about ${VAR}
# =============================================================================

import main as _main


@pytest.mark.parametrize("bad,reason", [
    ("${DEFINITELY_NOT_SET_12345}", "unset"),
    ("${lowercase_var}",            "lowercase"),
    ("Bearer ${TOKEN}",             "malformed"),
    ("${}",                         "malformed"),
])
def test_an_unresolvable_env_reference_is_refused(bad, reason, monkeypatch) -> None:
    """The audit finding, as an assertion.

    main._resolve_expect_substring treats ${VAR} as strict syntax and answers
    an unusable one with sys.exit(1) -- correct fail-fast for a file a human
    edits and tests locally. That file is now filled over HTTP, so a value the
    reader will refuse must not be accepted here: saving a typo from the
    dashboard returned 200 and then killed the monitor on its next run, every
    target and not just the edited one, with no alert about it because the
    process that sends alerts is the one that died.
    """
    monkeypatch.delenv("DEFINITELY_NOT_SET_12345", raising=False)
    with pytest.raises(ValidationError):
        validate_entry({"url": URL, "expect_substring": bad})


def test_a_resolvable_reference_is_accepted(monkeypatch) -> None:
    """The feature still works: the point is refusing what breaks, not
    refusing the syntax."""
    monkeypatch.setenv("HEALTH_TOKEN_TEST", "s3cret")
    entry = validate_entry({"url": URL, "expect_substring": "${HEALTH_TOKEN_TEST}"})
    assert entry["expect_substring"] == "${HEALTH_TOKEN_TEST}"


def test_a_plain_literal_is_untouched() -> None:
    """No "${" anywhere means it is a literal, and literals with a bare $ (a
    JSON "$schema" key, say) must keep working."""
    assert validate_entry(
        {"url": URL, "expect_substring": '"$schema":"ok"'}
    )["expect_substring"] == '"$schema":"ok"'


@pytest.mark.parametrize("value", [
    "${DEFINITELY_NOT_SET_12345}", "${lowercase_var}", "Bearer ${TOKEN}",
])
def test_whatever_the_store_accepts_the_probe_can_load(value, tmp_path, monkeypatch) -> None:
    """The invariant, asserted rather than assumed. Both sides answer the same
    question and only one of them answers it fatally, so the test proves they
    AGREE instead of proving each is self-consistent.

    Anything write_store accepts must survive load_targets without exiting.
    """
    monkeypatch.delenv("DEFINITELY_NOT_SET_12345", raising=False)
    store = tmp_path / "targets.json"
    try:
        write_store([{"url": URL, "expect_substring": value}], store)
    except ValidationError:
        return                      # refused up front: the reader never sees it

    try:
        _main.load_targets(store_path=store)
    except SystemExit:                                     # pragma: no cover
        pytest.fail(
            f"write_store accepted {value!r} and load_targets killed the "
            "process over it -- the two validators disagree"
        )


def test_both_sides_agree_on_what_the_syntax_IS() -> None:
    """Asserted as agreement in BEHAVIOUR, not as object identity.

    `_main._ENV_VAR_REF is targetstore.ENV_VAR_REF` looks like the stronger
    check and is the weaker one: re.compile caches, so restating the same
    pattern string hands back the very same object and the identity holds
    while the code is duplicated. What matters is that the two never disagree
    about a value, which is what this walks.
    """
    cases = [
        "${HEALTH_TOKEN}", "${lowercase}", "${}", "Bearer ${TOKEN}",
        "plain", '"$schema"', "${A1_B2}", "${1BAD}", "$ {SPACED}",
    ]
    for case in cases:
        mine   = targetstore.ENV_VAR_REF.match(case)
        theirs = _main._ENV_VAR_REF.match(case)
        assert (mine is None) == (theirs is None), case
        if mine:
            assert mine.group(1) == theirs.group(1), case
        assert bool(targetstore.ENV_VAR_SUSPECT.search(case)) == bool(
            _main._ENV_VAR_SUSPECT.search(case)), case


def test_a_lowercase_reference_is_refused_even_when_it_would_resolve(monkeypatch) -> None:
    """The case check earns its keep only when getenv WOULD find the variable.

    Windows resolves environment variables case-insensitively and Linux does
    not, so ${health_token} finds the value on a developer's machine and
    aborts in production -- the same targets.yaml behaving differently in two
    places. Testing it against an unset variable proves nothing, because the
    "unset" branch would refuse it anyway.
    """
    monkeypatch.setenv("CASE_PROBE_VAR", "value")
    with pytest.raises(ValidationError, match="MAY[UÚ]SCULAS"):
        validate_entry({"url": URL, "expect_substring": "${case_probe_var}"})


# =============================================================================
# F. The request body itself, before anything is parsed
# =============================================================================

import http.client

import api as _api


def test_deeply_nested_json_answers_400_not_a_dead_socket(live) -> None:
    """RecursionError is NOT a JSONDecodeError, so it escaped do_PUT entirely:
    the handler thread died and the client got a connection reset instead of a
    refusal. A few KB of "[[[[" is enough -- far under MAX_BODY_BYTES -- and it
    is reachable WITHOUT a token, because the body is read before the auth
    check by design (so that a 401 does not reset the socket)."""
    host, port = live.removeprefix("http://").split(":")
    payload = ("[" * 3000 + "]" * 3000).encode()
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("PUT", "/api/targets", body=payload,
                 headers={"Content-Length": str(len(payload))})
    resp = conn.getresponse()
    assert resp.status == 400
    assert "anidado" in _json.loads(resp.read())["error"]


@pytest.mark.parametrize("length", ["-1", "-999999", "not-a-number"])
def test_a_hostile_content_length_is_refused(live, length) -> None:
    """`-1 > MAX_BODY_BYTES` is False, so a negative length sailed past the
    size guard and reached read(-1) -- which means "read until EOF", removing
    the limit by way of the check meant to enforce it."""
    host, port = live.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.putrequest("PUT", "/api/targets")
    conn.putheader("Content-Length", length)
    conn.endheaders()
    assert conn.getresponse().status == 400


def test_the_handler_has_a_socket_timeout() -> None:
    """Without it, socketserver never calls settimeout() and a client that
    announces a body it never sends holds a thread forever. ThreadingHTTPServer
    caps nothing, so enough of those starve the process serving the dashboard
    and both read-only endpoints."""
    assert _api._Handler.timeout is not None
    assert 0 < _api._Handler.timeout <= 60
