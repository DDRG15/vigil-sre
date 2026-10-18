"""
tests/test_env_isolation.py -- the suite must not reach a real endpoint.

Why this file exists
--------------------
main.py calls load_dotenv() when it is imported, and every test module imports
it. On a developer machine that copies the real .env into the process before the
first test runs. Measured: one full run sent 13 heartbeat pings
to the HEARTBEAT_URL in the real .env. An external watchdog read them as 13
proofs of life while the monitor under test may have been dead, and a webhook
in the same file would have posted test alerts to a real channel.

The fix lives in conftest.py and these tests hold it. The last one is the
measurement itself, shrunk: a child pytest, an environment full of live-looking
URLs, a receiver counting what arrives.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from dotenv import load_dotenv

# The import every other test module makes, and the one that runs load_dotenv().
# Without it this file can be collected alone and pass for the wrong reason.
import main  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
CHANNEL_VARS = ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "HEARTBEAT_URL")


def test_no_test_starts_with_a_webhook_configured() -> None:
    leaked = {v: os.environ[v] for v in CHANNEL_VARS if os.environ.get(v)}
    assert not leaked, f"the environment hands every test a live channel: {sorted(leaked)}"


def test_a_dotenv_cannot_fill_the_blank_back_in(tmp_path: Path) -> None:
    """The fixture has to survive main.py's own load_dotenv(), which runs at
    import and again in any child process. load_dotenv(override=False) skips a
    variable that is PRESENT, even as an empty string, and fills one that is
    absent -- so removing the variables would not hold and blanking them does."""
    fake = tmp_path / ".env"
    fake.write_text(
        "DISCORD_WEBHOOK_URL=http://127.0.0.1:9/discord\n"
        "SLACK_WEBHOOK_URL=http://127.0.0.1:9/slack\n"
        "HEARTBEAT_URL=http://127.0.0.1:9/heartbeat\n", encoding="utf-8")

    load_dotenv(fake)

    leaked = {v: os.environ[v] for v in CHANNEL_VARS if os.environ.get(v)}
    assert not leaked, f"a .env repopulated the channels: {sorted(leaked)}"


def test_a_child_started_without_the_channels_gets_them_blank() -> None:
    """Three tests run `python main.py` with an environment copied from
    os.environ minus the channel variables. Absent is what lets the child's own
    load_dotenv() refill them from the real .env, so the suite hands over blanks
    instead. A URL the test names itself is left alone."""
    env = {k: v for k, v in os.environ.items() if k not in CHANNEL_VARS}
    env["SLACK_WEBHOOK_URL"] = "http://127.0.0.1:9/chosen"
    code = ("import os; print(*(repr(os.environ.get(v)) for v in "
            f"{CHANNEL_VARS!r}), sep='|')")

    child = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=60)

    assert child.stdout.strip() == "''|'http://127.0.0.1:9/chosen'|''", child.stderr


@pytest.fixture(scope="module")
def child_started_by_a_module_fixture() -> subprocess.CompletedProcess:
    """test_alert_kinds spawns its `python main.py` from a module-scoped
    fixture, which pytest sets up before any function-scoped one."""
    env = {k: v for k, v in os.environ.items() if k not in CHANNEL_VARS}
    code = ("import os; print(*(repr(os.environ.get(v)) for v in "
            f"{CHANNEL_VARS!r}), sep='|')")
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=60)


def test_a_child_from_a_module_scoped_fixture_gets_them_blank(
        child_started_by_a_module_fixture) -> None:
    assert child_started_by_a_module_fixture.stdout.strip() == "''|''|''"


_PROBE = textwrap.dedent('''
    """Everything a monitor run would send, aimed at whatever the env says."""
    import asyncio

    import aiohttp

    import main
    import notifiers


    def test_the_heartbeat_goes_nowhere():
        asyncio.run(main._send_heartbeat(0))


    def test_an_alert_goes_nowhere():
        async def send():
            async with aiohttp.ClientSession() as session:
                await notifiers.dispatch_alert(
                    session, "https://example.invalid/", "probe",
                    next(iter(notifiers.AlertKind)))
        asyncio.run(send())
''')


class _Sink(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).hits.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args) -> None:
        return


def test_a_full_environment_of_live_urls_reaches_no_endpoint(tmp_path: Path) -> None:
    """A child pytest with this repo's conftest, an environment that names all
    three channels, and a sink counting arrivals. Unfixed, the heartbeat and the
    alert both land; fixed, the sink hears nothing."""
    sink = type("Sink", (_Sink,), {"hits": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    shutil.copy(REPO / "tests" / "conftest.py", project / "tests" / "conftest.py")
    (project / "tests" / "test_probe.py").write_text(_PROBE, encoding="utf-8")

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "DISCORD_WEBHOOK_URL": f"{base}/discord",
        "SLACK_WEBHOOK_URL": f"{base}/slack",
        "HEARTBEAT_URL": f"{base}/heartbeat",
        "TARGETS_STORE_PATH": str(project / "nostore.json"),
    }
    try:
        child = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
            cwd=project, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
    finally:
        server.shutdown()
        server.server_close()

    assert child.returncode == 0, child.stdout[-1500:]
    assert sink.hits == [], f"the suite reached a configured endpoint: {sink.hits}"
