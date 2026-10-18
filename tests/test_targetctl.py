"""
tests/test_targetctl.py — the terminal path to the target list must not lose any.

What is actually being defended
-------------------------------
targetctl.py edits the list by reading it whole and writing it whole. That
shape has one way to hurt: a write built from less than was read. A CLI that
sent only the new target, or rebuilt each entry from the few fields it
understands, would replace the monitor's list with a fragment and answer 200.
The operator would find out when a silenced target paged at 3 AM.

The server in these tests is the real api.py on a free port, writing a real
store in tmp_path. Nothing about the API is faked, so a 400, a 401 and the
seed-to-store transition are the API's own answers rather than the test's
opinion of them. The server counts what reaches it, which is how "nothing was
sent" is proved from the receiving side instead of asserted from the sending
one.

The token lives only in the CLI's environment: api.write_token is pinned, so
deleting API_WRITE_TOKEN here removes the client's token without also removing
the server's.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api
import targetctl
import targetstore

TOKEN = "test-token-not-a-real-secret"
WRONG = "wrong-token-also-not-a-secret"

#: Every field the store accepts, so a field the CLI knows nothing about is
#: present on the target the tests must not damage.
RICH = {
    "url": "https://rich.example/health",
    "expect_substring": "${HEALTH_TOKEN}",
    "expected_status": 204,
    "timeout_s": 10.0,
    "degraded_ttfb_ms": 3000.0,
    "degraded_rtt_ms": 250.0,
    "degraded_backend_ms": 2000.0,
    "remind": True,
    "trend_alerts": False,
    "maintenance": [{"days": [0, 6], "start": "02:00", "end": "04:00"}],
}

SEED_YAML = """\
targets:
  - https://plain.example
  - url: https://seed-rtt.example
    degraded_rtt_ms: 400
    trend_alerts: false
"""


@pytest.fixture(autouse=True)
def _clean_cli_environment(tmp_path: Path, monkeypatch) -> None:
    """A cwd with no .env, a known token, and no inherited API address."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIGIL_API", raising=False)
    monkeypatch.setenv("API_WRITE_TOKEN", TOKEN)
    monkeypatch.setenv("HEALTH_TOKEN", "present")
    monkeypatch.setattr(targetstore, "SEED_FILE", tmp_path / "seed.yaml")
    monkeypatch.setattr(targetstore, "STORE_FILE", tmp_path / "data" / "targets.json")


@pytest.fixture
def seen(monkeypatch) -> list[tuple[str, str]]:
    """Every (method, path) the API received. Appended by the server itself."""
    log: list[tuple[str, str]] = []
    real = api._Handler.log_message

    def record(self, fmt, *args):
        log.append((self.command, self.path))
        real(self, fmt, *args)

    monkeypatch.setattr(api._Handler, "log_message", record)
    return log


@pytest.fixture
def server(tmp_path: Path, monkeypatch, seen):
    monkeypatch.setattr(api, "write_token", lambda: TOKEN)
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


def run(capsys, server: str, *argv: str) -> tuple[int, str, str]:
    code = targetctl.main(["--api", server, *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def seed_file(text: str = SEED_YAML) -> None:
    targetstore.SEED_FILE.write_text(text, encoding="utf-8")


def save(entries: list[dict]) -> None:
    """A previous dashboard save: the store exists and holds *entries*."""
    targetstore.write_store(entries)


def stored() -> list[dict] | None:
    return targetstore.read_store()


def writes(seen: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [call for call in seen if call[0] == "PUT"]


# =============================================================================
# list
# =============================================================================


def test_list_from_the_seed_says_it_is_not_managed(server, capsys) -> None:
    seed_file()
    code, out, err = run(capsys, server, "list")

    assert (code, err) == (0, "")
    assert "targets.yaml seed" in out and "not yet managed" in out
    assert "https://plain.example" in out
    assert "https://seed-rtt.example  degraded_rtt_ms=400.0 trend_alerts=false" in out
    assert not targetstore.STORE_FILE.exists()


def test_list_from_the_store_says_it_is_managed(server, capsys) -> None:
    seed_file()
    save([RICH, {"url": "https://second.example"}])

    code, out, err = run(capsys, server, "list")

    assert (code, err) == (0, "")
    assert "dashboard store" in out and "managed" in out
    assert "not yet managed" not in out
    assert "2 target(s)" in out
    assert "https://rich.example/health" in out
    assert "expected_status=204" in out and "remind=true" in out
    # Key order inside a window is the store's, not the test's to dictate.
    assert "maintenance=[{" in out and '"days":[0,6]' in out and '"start":"02:00"' in out
    # The seed is not consulted once the store exists, and neither is its list.
    assert "https://plain.example" not in out


def test_list_needs_no_token(server, capsys, monkeypatch) -> None:
    seed_file()
    monkeypatch.delenv("API_WRITE_TOKEN")
    code, out, _ = run(capsys, server, "list")
    assert code == 0 and "https://plain.example" in out


def test_list_reports_a_seed_that_cannot_be_read(server, capsys) -> None:
    """An empty list with no explanation reads as 'nothing is monitored'."""
    code, out, err = run(capsys, server, "list")  # no seed file, no store
    assert code == 1
    assert "seed cannot be shown" in err
    assert "Traceback" not in err


def test_api_flag_works_after_the_subcommand_and_via_the_environment(
    server, capsys, monkeypatch
) -> None:
    seed_file()
    assert targetctl.main(["list", "--api", server]) == 0
    capsys.readouterr()

    monkeypatch.setenv("VIGIL_API", server)
    assert targetctl.main(["list"]) == 0
    assert "https://plain.example" in capsys.readouterr().out


# =============================================================================
# add
# =============================================================================


def test_first_add_moves_the_seed_into_the_store_and_says_so(
    server, capsys, seen
) -> None:
    seed_file()
    code, out, err = run(capsys, server, "add", "https://new.example",
                         "--set", "timeout_s=5", "remind=true")

    assert (code, err) == (0, "")
    assert "Added https://new.example. 3 target(s) now." in out
    assert "dashboard store" in out and "will not be read" in out

    urls = [e["url"] for e in stored()]
    assert urls == ["https://plain.example", "https://seed-rtt.example",
                    "https://new.example"]
    new = stored()[-1]
    assert new["timeout_s"] == 5.0 and new["remind"] is True
    # The seed entries came through the transition with their settings.
    assert stored()[1]["degraded_rtt_ms"] == 400.0
    assert stored()[1]["trend_alerts"] is False
    assert [m for m, _ in writes(seen)] == ["PUT"]


def test_add_to_a_managed_list_has_no_seed_notice(server, capsys) -> None:
    save([{"url": "https://a.example"}])
    code, out, _ = run(capsys, server, "add", "https://b.example")
    assert code == 0
    assert "will not be read" not in out
    assert [e["url"] for e in stored()] == ["https://a.example", "https://b.example"]


def test_add_refuses_a_duplicate_and_sends_nothing(server, capsys, seen) -> None:
    save([{"url": "https://a.example"}])
    code, out, err = run(capsys, server, "add", "  https://a.example  ")

    assert code == 1 and out == ""
    assert "already a target" in err and err.count("\n") == 1
    assert writes(seen) == []
    assert [e["url"] for e in stored()] == ["https://a.example"]


def test_a_field_the_cli_does_not_know_survives_add_and_remove(
    server, capsys
) -> None:
    """The regression that motivated the rule: an editor that rewrote entries
    from the fields it rendered deleted maintenance windows and opt-ins."""
    other = {"url": "https://other.example", "remind": True}
    save([RICH, other])
    before = {e["url"]: e for e in stored()}
    assert before[RICH["url"]]["maintenance"] and before[RICH["url"]]["trend_alerts"] is False

    assert run(capsys, server, "add", "https://third.example")[0] == 0
    after_add = {e["url"]: e for e in stored()}
    assert after_add[RICH["url"]] == before[RICH["url"]]
    assert after_add[other["url"]] == before[other["url"]]
    assert len(after_add) == 3

    assert run(capsys, server, "remove", "https://third.example")[0] == 0
    after_remove = {e["url"]: e for e in stored()}
    assert after_remove == before


def test_add_writes_the_whole_list_not_the_changed_entry(server, capsys) -> None:
    save([{"url": f"https://t{i}.example"} for i in range(5)])
    assert run(capsys, server, "add", "https://t5.example")[0] == 0
    assert len(stored()) == 6


def test_invalid_setting_value_surfaces_the_apis_400_verbatim(
    server, capsys, seen
) -> None:
    save([{"url": "https://a.example"}])
    code, out, err = run(capsys, server, "add", "https://b.example",
                         "--set", "timeout_s=0")

    assert code == 1 and out == ""
    assert "400" in err
    assert "'timeout_s' must be between 0.1 and 300." in err
    assert err.count("\n") == 1
    assert [e["url"] for e in stored()] == ["https://a.example"]


@pytest.mark.parametrize("sets, message", [
    (["timeout_s=abc"], "'timeout_s' must be a number."),
    (["remind=maybe"], "'remind' must be true or false."),
    (["maintenance=nonsense"], "'maintenance' must be a list of windows."),
])
def test_untypable_values_reach_the_api_as_text_and_come_back_as_its_400(
    server, capsys, sets, message
) -> None:
    save([{"url": "https://a.example"}])
    code, _, err = run(capsys, server, "add", "https://b.example", "--set", *sets)
    assert code == 1 and message in err


def test_a_url_the_store_refuses_is_the_apis_400(server, capsys) -> None:
    save([{"url": "https://a.example"}])
    code, _, err = run(capsys, server, "add", "ftp://files.example")
    assert code == 1
    assert "Scheme 'ftp' is not allowed" in err


@pytest.mark.parametrize("bad", ["bogus=1", "noequals", "=5", "url=https://x.example"])
def test_a_malformed_or_unknown_setting_is_refused_before_any_request(
    server, capsys, seen, bad
) -> None:
    save([{"url": "https://a.example"}])
    code, out, err = run(capsys, server, "add", "https://b.example", "--set", bad)

    assert code == 1 and out == ""
    assert "bad --set" in err and err.count("\n") == 1
    assert seen == []


def test_the_same_setting_twice_is_refused(server, capsys, seen) -> None:
    code, _, err = run(capsys, server, "add", "https://b.example",
                       "--set", "timeout_s=5", "timeout_s=6")
    assert code == 1 and "given twice" in err and seen == []


def test_set_values_are_typed_before_sending() -> None:
    parsed = targetctl.parse_settings([
        "timeout_s=5", "expected_status=204", "remind=TRUE",
        "trend_alerts=false", "expect_substring=ok",
        'maintenance=[{"start":"01:00","end":"02:00"}]',
    ])
    assert parsed == {
        "timeout_s": 5, "expected_status": 204, "remind": True,
        "trend_alerts": False, "expect_substring": "ok",
        "maintenance": [{"start": "01:00", "end": "02:00"}],
    }
    assert isinstance(parsed["timeout_s"], int)
    assert targetctl.parse_settings(["timeout_s=2.5"]) == {"timeout_s": 2.5}
    # NaN is not JSON; it must travel as text and be refused by the API.
    assert targetctl.parse_settings(["timeout_s=nan"]) == {"timeout_s": "nan"}


# =============================================================================
# remove
# =============================================================================


def test_remove_deletes_one_target_and_keeps_the_rest(server, capsys) -> None:
    save([{"url": "https://a.example"}, RICH, {"url": "https://c.example"}])
    code, out, err = run(capsys, server, "remove", "https://a.example")

    assert (code, err) == (0, "")
    assert "Removed https://a.example. 2 target(s) now." in out
    assert [e["url"] for e in stored()] == [RICH["url"], "https://c.example"]


def test_remove_from_the_seed_also_hands_the_list_to_the_store(
    server, capsys
) -> None:
    seed_file()
    code, out, _ = run(capsys, server, "remove", "https://plain.example")
    assert code == 0 and "will not be read" in out
    assert [e["url"] for e in stored()] == ["https://seed-rtt.example"]


def test_remove_of_an_unknown_url_is_refused_and_sends_nothing(
    server, capsys, seen
) -> None:
    save([{"url": "https://a.example"}])
    code, out, err = run(capsys, server, "remove", "https://nope.example")
    assert code == 1 and out == ""
    assert "is not a target" in err and err.count("\n") == 1
    assert writes(seen) == []


def test_removing_the_last_target_needs_allow_empty_and_sends_nothing_without(
    server, capsys, seen
) -> None:
    save([{"url": "https://only.example"}])
    code, out, err = run(capsys, server, "remove", "https://only.example")

    assert code == 1 and out == ""
    assert "last target" in err and "--allow-empty" in err
    assert writes(seen) == []
    assert [e["url"] for e in stored()] == ["https://only.example"]


def test_allow_empty_removes_the_last_target_and_the_store_stays_managed(
    server, capsys
) -> None:
    save([{"url": "https://only.example"}])
    code, out, _ = run(capsys, server, "remove", "https://only.example",
                       "--allow-empty")

    assert code == 0 and "0 target(s) now" in out
    # [] and None are different answers: [] must not fall back to the seed.
    assert stored() == []


# =============================================================================
# token, 401, API down
# =============================================================================


@pytest.mark.parametrize("command", [
    ["add", "https://b.example"],
    ["remove", "https://a.example"],
])
@pytest.mark.parametrize("blank", [False, True])
def test_a_write_without_a_token_exits_1_and_sends_no_request(
    server, capsys, seen, monkeypatch, command, blank
) -> None:
    save([{"url": "https://a.example"}, {"url": "https://z.example"}])
    if blank:
        monkeypatch.setenv("API_WRITE_TOKEN", "")
    else:
        monkeypatch.delenv("API_WRITE_TOKEN")

    code, out, err = run(capsys, server, *command)

    assert code == 1 and out == ""
    assert "API_WRITE_TOKEN is not set" in err and err.count("\n") == 1
    assert seen == []                       # not even the GET
    assert len(stored()) == 2


def test_a_wrong_token_is_a_401_that_names_the_cause_and_hides_the_token(
    server, capsys, monkeypatch
) -> None:
    save([{"url": "https://a.example"}])
    monkeypatch.setenv("API_WRITE_TOKEN", WRONG)

    code, out, err = run(capsys, server, "add", "https://b.example")

    assert code == 1 and out == ""
    assert "401" in err and "write not authorised" in err
    assert WRONG not in out + err and TOKEN not in out + err
    assert err.count("\n") == 1
    assert [e["url"] for e in stored()] == ["https://a.example"]


def test_the_token_is_never_printed_on_success(server, capsys) -> None:
    save([{"url": "https://a.example"}])
    _, out, err = run(capsys, server, "add", "https://b.example")
    assert TOKEN not in out + err


def test_the_token_is_sent_on_the_write_and_not_on_the_read(
    server, capsys, monkeypatch
) -> None:
    save([{"url": "https://a.example"}])
    sent: list[tuple[str, str | None]] = []
    real = urllib.request.OpenerDirector.open

    def spy(self, req, *args, **kwargs):
        sent.append((req.get_method(), req.get_header("X-vigil-token")))
        return real(self, req, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
    assert run(capsys, server, "add", "https://b.example")[0] == 0
    assert sent == [("GET", None), ("PUT", TOKEN)]


def test_a_token_that_cannot_be_a_header_is_refused_without_echoing_it(
    server, capsys, seen, monkeypatch
) -> None:
    monkeypatch.setenv("API_WRITE_TOKEN", "abc\r\nX-Evil: 1")
    code, _, err = run(capsys, server, "add", "https://b.example")
    assert code == 1 and "cannot be sent as a header" in err
    assert "X-Evil" not in err and seen == []


def test_api_down_is_one_line_with_no_traceback(capsys) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The socket is closed again: nothing listens on `port`.

    code, out, err = run(capsys, f"http://127.0.0.1:{port}", "list")

    assert code == 1 and out == ""
    assert "cannot reach the API" in err
    assert err.count("\n") == 1 and "Traceback" not in err


@pytest.mark.parametrize("address", ["file:///etc/passwd", "ftp://x", "localhost:8787", ""])
def test_a_non_http_api_address_is_refused(capsys, address) -> None:
    code = targetctl.main(["--api", address or "  ", "list"])
    err = capsys.readouterr().err
    assert code == 1 and "bad API address" in err


def test_something_that_is_not_the_api_is_named_as_such(capsys) -> None:
    class Teapot(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Teapot)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        code, _, err = run(capsys, f"http://127.0.0.1:{srv.server_address[1]}", "list")
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
    assert code == 1 and "not JSON" in err and err.count("\n") == 1


def test_an_unreadable_seed_blocks_the_first_write(server, capsys, seen) -> None:
    """GET answers [] plus a seed_error for a seed it cannot read. PUTting that
    list with one URL appended would replace every real target with one."""
    assert not targetstore.SEED_FILE.exists() and not targetstore.STORE_FILE.exists()
    code, out, err = run(capsys, server, "add", "https://b.example")

    assert code == 1 and out == ""
    assert "cannot be read from the seed" in err and "Nothing was written" in err
    assert writes(seen) == []
    assert not targetstore.STORE_FILE.exists()


# =============================================================================
# validate
# =============================================================================


@pytest.fixture
def offline(monkeypatch):
    """validate must not touch the network."""
    def refuse(*args, **kwargs):
        raise AssertionError("validate made a network request")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_validate_accepts_a_good_yaml_file(offline, capsys, tmp_path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(SEED_YAML, encoding="utf-8")
    code = targetctl.main(["validate", str(good)])
    out = capsys.readouterr()
    assert code == 0 and "OK" in out.out and "2 valid target(s)" in out.out


def test_validate_defaults_to_the_seed_file(offline, capsys) -> None:
    seed_file()
    assert targetctl.main(["validate"]) == 0
    assert "seed.yaml" in capsys.readouterr().out


def test_validate_accepts_a_store_shaped_json_file(offline, capsys, tmp_path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"targets": [RICH]}), encoding="utf-8")
    assert targetctl.main(["validate", str(path)]) == 0


@pytest.mark.parametrize("name, text, message", [
    ("unknown.yaml", "targets:\n  - url: https://a.example\n    colour: red\n",
     "Unrecognised field(s): colour"),
    ("scheme.yaml", "targets:\n  - file:///etc/passwd\n", "Scheme 'file' is not allowed"),
    ("dup.yaml", "targets:\n  - https://a.example\n  - https://a.example\n",
     "Duplicate target: https://a.example"),
    ("range.yaml", "targets:\n  - url: https://a.example\n    timeout_s: 0\n",
     "'timeout_s' must be between"),
    ("nourl.yaml", "targets:\n  - timeout_s: 5\n", "'url' is required"),
    ("empty.yaml", "targets: []\n", "list is empty"),
    ("notlist.yaml", "targets: nope\n", "expected a 'targets' list"),
    ("broken.yaml", "targets: [unclosed\n", "not valid YAML"),
    ("broken.json", "{not json", "not valid JSON"),
])
def test_validate_rejects_a_bad_file_with_one_line(
    offline, capsys, tmp_path, name, text, message
) -> None:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    code = targetctl.main(["validate", str(path)])
    err = capsys.readouterr().err
    assert code == 1 and message in err
    assert err.count("\n") == 1 and "Traceback" not in err


def test_validate_of_a_missing_file_is_a_clean_failure(offline, capsys, tmp_path) -> None:
    code = targetctl.main(["validate", str(tmp_path / "absent.yaml")])
    err = capsys.readouterr().err
    assert code == 1 and "cannot read" in err and "Traceback" not in err


def test_validate_checks_env_references_only_when_asked(
    offline, capsys, tmp_path, monkeypatch
) -> None:
    path = tmp_path / "env.yaml"
    path.write_text(
        "targets:\n  - url: https://a.example\n    expect_substring: ${NOT_SET_ANYWHERE}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)

    assert targetctl.main(["validate", str(path)]) == 0
    capsys.readouterr()
    assert targetctl.main(["validate", str(path), "--check-env"]) == 1
    assert "NOT_SET_ANYWHERE" in capsys.readouterr().err


# =============================================================================
# as a program: __main__, .env, exit codes
# =============================================================================


def test_the_script_reads_its_token_from_dotenv_and_exits_cleanly(
    server, tmp_path
) -> None:
    save([{"url": "https://a.example"}])
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / ".env").write_text(f"API_WRITE_TOKEN={TOKEN}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "API_WRITE_TOKEN"}
    env["VIGIL_API"] = server

    added = subprocess.run(
        [sys.executable, str(ROOT / "targetctl.py"), "add", "https://b.example",
         "--set", "timeout_s=5"],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=60,
    )
    assert added.returncode == 0, added.stderr
    assert [e["url"] for e in stored()] == ["https://a.example", "https://b.example"]
    assert TOKEN not in added.stdout + added.stderr

    # Without the .env the same command stops before sending anything.
    (workdir / ".env").unlink()
    refused = subprocess.run(
        [sys.executable, str(ROOT / "targetctl.py"), "remove", "https://b.example"],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=60,
    )
    assert refused.returncode == 1
    assert "API_WRITE_TOKEN is not set" in refused.stderr
    assert "Traceback" not in refused.stderr
    assert len(stored()) == 2


def test_a_usage_error_exits_2(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        targetctl.main(["frobnicate"])
    assert exc.value.code == 2
