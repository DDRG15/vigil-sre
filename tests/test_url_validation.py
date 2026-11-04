"""
tests/test_url_validation.py — a URL the probe cannot dial is refused when it
is saved, not when it crashes the sampler.

validate_entry used to check only the scheme and a non-empty netloc. So
`http://example.com:99999/`, `http://host:80a0/` and `https://example..com/`
were accepted by the API and by targets.yaml, and then raised inside the probe
on every round. The same rule now runs on every door: the store, the YAML seed
(strings and objects), the API and `targetctl validate`.

A target URL is also refused when it carries a credential (userinfo, or a
query parameter named like one): it is stored, logged and sent in alerts.

Deliberately NOT tested as invalid: private, loopback and link-local addresses.
Whether a monitor may probe the inside of its own network is a policy decision,
not a syntax rule.
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
from targetstore import ValidationError, validate_all, validate_entry

TOKEN = "test-token-not-a-real-secret"

BAD_URLS = [
    pytest.param("http://example.com:99999/", "port", id="port-out-of-range"),
    pytest.param("http://host:80a0/", "port", id="port-not-a-number"),
    pytest.param("http://host:0/", "port", id="port-zero"),
    pytest.param("http://host:-1/", "port", id="port-negative"),
    pytest.param("https://example..com/", "domain", id="empty-label"),
    pytest.param("http://./", "domain", id="dot-only-host"),
    pytest.param("http://" + "a" * 64 + ".com/", "domain", id="label-over-63"),
    pytest.param("http://:8080/", "no host", id="port-without-host"),
    pytest.param("http://user@/", "no host", id="userinfo-without-host"),
    pytest.param("http://[::1/", "", id="unclosed-ipv6-bracket"),
]

GOOD_URLS = [
    "https://example.com",
    "https://example.com/health?x=1",
    "http://localhost:8080/",
    "http://127.0.0.1:65535/",
    "http://[::1]:8080/",
    "http://[::1]/",
    "http://host:/",                    # an empty port means the default
    "http://example.com./",             # a fully-qualified trailing dot
    "http://my_host.internal/",         # underscores exist in internal DNS
    "https://bücher.de/",               # needs IDNA, and has it
    "http://10.0.0.5:8443/",            # private addresses are not this rule's business
]


# =============================================================================
# A. validate_entry
# =============================================================================


@pytest.mark.parametrize("url,word", BAD_URLS)
def test_a_url_the_probe_cannot_dial_is_refused(url, word) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_entry({"url": url})
    assert word in str(exc.value).lower()
    assert "\n" not in str(exc.value)


@pytest.mark.parametrize("url", GOOD_URLS)
def test_a_dialable_url_is_still_accepted(url) -> None:
    assert validate_entry({"url": url})["url"] == url


def test_the_existing_scheme_and_host_messages_are_unchanged() -> None:
    with pytest.raises(ValidationError, match="Scheme 'file'"):
        validate_entry({"url": "file:///etc/passwd"})
    with pytest.raises(ValidationError, match="no host"):
        validate_entry({"url": "http://"})


def test_the_whole_list_is_refused_when_one_entry_is_bad() -> None:
    with pytest.raises(ValidationError):
        validate_all([{"url": "https://ok.example"}, {"url": "http://host:99999/"}])


# --- Credentials in the URL ----------------------------------------------------
#
# A target URL is the target's identity: stored, logged on every check, returned
# by the API and MCP, exported, and written into Discord and Slack alerts. A
# credential in it reaches all of those, so it is refused where it is entered.

CREDENTIAL_URLS = [
    pytest.param("https://u:p@example.invalid/", id="user-and-password"),
    pytest.param("https://u@example.invalid/", id="user-only"),
    pytest.param("https://:p@example.invalid/", id="password-only"),
    pytest.param("https://example.invalid/?token=x", id="token"),
    pytest.param("https://example.invalid/?Access_Token=x", id="access-token-mixed-case"),
    pytest.param("https://example.invalid/?api_key=x", id="api-key"),
    pytest.param("https://example.invalid/?x_api_key=x", id="suffix-_key"),
    pytest.param("https://example.invalid/?x-api-key=x", id="suffix--key"),
    pytest.param("https://example.invalid/?sig=x", id="sig"),
    pytest.param("https://example.invalid/?page=2&token=x", id="credential-after-a-plain-param"),
    pytest.param("https://example.invalid/?token=", id="credential-with-empty-value"),
]

NOT_CREDENTIAL_URLS = [
    pytest.param("https://example.invalid/?page=2", id="page"),
    pytest.param("https://example.invalid/?monkey=1", id="ends-in-key-but-not-_key"),
    pytest.param("https://example.invalid/?tokens_total=1", id="token-inside-a-name"),
    pytest.param("https://example.invalid/?sort=asc&limit=10", id="several-plain-params"),
    pytest.param("https://example.invalid/a@b", id="at-sign-in-the-path"),
]


@pytest.mark.parametrize("url", CREDENTIAL_URLS)
def test_a_url_carrying_a_credential_is_refused(url) -> None:
    """A user, a password or a credential-named query parameter in a target URL
    would be stored, logged and sent to Discord/Slack in every alert."""
    with pytest.raises(ValidationError) as exc:
        validate_entry({"url": url})
    assert "credential" in str(exc.value).lower()
    assert "\n" not in str(exc.value)


@pytest.mark.parametrize("url", NOT_CREDENTIAL_URLS)
def test_a_url_with_ordinary_parameters_is_still_accepted(url) -> None:
    """Over-blocking is a failure too: 'monkey' ends in 'key' and 'tokens_total'
    contains 'token', and neither is a credential name."""
    assert validate_entry({"url": url})["url"] == url


def test_the_refusal_does_not_repeat_the_credential_value() -> None:
    """The message goes to logs and the API's 400 body; echoing the value
    would put the secret exactly where the rule keeps it out of."""
    with pytest.raises(ValidationError) as exc:
        validate_entry({"url": "https://u:AUDIT_FAKE_TOKEN@example.invalid/"})
    assert "AUDIT_FAKE_TOKEN" not in str(exc.value)
    with pytest.raises(ValidationError) as exc:
        validate_entry({"url": "https://example.invalid/?token=AUDIT_FAKE_TOKEN"})
    assert "AUDIT_FAKE_TOKEN" not in str(exc.value)


def test_the_whole_list_is_refused_when_one_url_carries_a_credential() -> None:
    with pytest.raises(ValidationError):
        validate_all([{"url": "https://ok.example"}, {"url": "https://u:p@example.invalid/"}])


# =============================================================================
# B. The YAML seed, through load_targets
# =============================================================================


def _seed(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "targets.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize("url,word", BAD_URLS)
@pytest.mark.parametrize("form", ["string", "object"])
def test_load_targets_refuses_a_bad_url_in_either_yaml_form(
        tmp_path: Path, url, word, form, caplog) -> None:
    quoted = json.dumps(url)
    entry = f"  - {quoted}\n" if form == "string" else f"  - url: {quoted}\n"
    path = _seed(tmp_path, "targets:\n  - https://ok.example\n" + entry)

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as exit_info:
            main.load_targets(path, store_path=tmp_path / "no-store.json")

    assert exit_info.value.code == 1
    assert str(path) in caplog.text
    if "user@" in url:
        assert "user@" not in caplog.text and "no host" in caplog.text
    elif "[" in url:
        assert "[invalid URL]" in caplog.text
    else:
        assert url in caplog.text, "the rejected host and path remain identifiable"


@pytest.mark.parametrize("url", CREDENTIAL_URLS)
@pytest.mark.parametrize("form", ["string", "object"])
def test_load_targets_refuses_a_credential_url_in_either_yaml_form(
        tmp_path: Path, url, form) -> None:
    """The YAML seed is a door like the API: a credential refused there and
    accepted here would still reach state.json, the logs and the alerts."""
    quoted = json.dumps(url)
    entry = f"  - {quoted}\n" if form == "string" else f"  - url: {quoted}\n"
    path = _seed(tmp_path, "targets:\n  - https://ok.example\n" + entry)

    with pytest.raises(SystemExit) as exit_info:
        main.load_targets(path, store_path=tmp_path / "no-store.json")

    assert exit_info.value.code == 1


@pytest.mark.parametrize("url", NOT_CREDENTIAL_URLS)
def test_load_targets_keeps_loading_urls_with_ordinary_parameters(tmp_path: Path, url) -> None:
    path = _seed(tmp_path, f"targets:\n  - {json.dumps(url)}\n")
    got = main.load_targets(path, store_path=tmp_path / "no-store.json")
    assert [t.url for t in got] == [url]


@pytest.mark.parametrize("url", GOOD_URLS)
def test_load_targets_keeps_loading_dialable_urls(tmp_path: Path, url) -> None:
    path = _seed(tmp_path, f"targets:\n  - {json.dumps(url)}\n")
    got = main.load_targets(path, store_path=tmp_path / "no-store.json")
    assert [t.url for t in got] == [url]


# =============================================================================
# C. The API
# =============================================================================


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "data" / "targets.json")
    monkeypatch.setattr(targetstore, "SEED_FILE", tmp_path / "targets.yaml")
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


def _put(base: str, targets: list):
    request = urllib.request.Request(
        base + "/api/targets", method="PUT",
        data=json.dumps({"targets": targets}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Vigil-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.mark.parametrize("url,word", BAD_URLS)
def test_the_api_answers_400_and_stores_nothing(server, url, word) -> None:
    status, body = _put(server, [{"url": url}])

    assert status == 400
    assert word in body["error"].lower()
    assert not targetstore.STORE_FILE.exists()


@pytest.mark.parametrize("url", CREDENTIAL_URLS)
def test_the_api_answers_400_for_a_credential_url_and_stores_nothing(server, url) -> None:
    status, body = _put(server, [{"url": url}])

    assert status == 400
    assert "credential" in body["error"].lower()
    assert not targetstore.STORE_FILE.exists()


def test_the_api_accepts_an_explicit_port_and_an_ipv6_literal(server) -> None:
    status, body = _put(server, [
        {"url": "http://[::1]:8080/"}, {"url": "https://example.com:8443/x"}])
    assert status == 200 and len(body["targets"]) == 2


# =============================================================================
# D. targetctl validate
# =============================================================================


@pytest.mark.parametrize("url,word", BAD_URLS)
@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_targetctl_validate_refuses_a_bad_url(
        tmp_path: Path, monkeypatch, capsys, url, word, suffix) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / f"targets{suffix}"
    if suffix == ".json":
        path.write_text(json.dumps({"targets": [{"url": url}]}), encoding="utf-8")
    else:
        path.write_text(f"targets:\n  - {json.dumps(url)}\n", encoding="utf-8")

    code = targetctl.main(["validate", str(path)])
    err = capsys.readouterr().err

    assert code == 1
    assert word in err.lower()
