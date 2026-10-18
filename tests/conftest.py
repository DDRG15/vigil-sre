"""
tests/conftest.py — compatibility shim between aioresponses and aiohttp 3.14+.

Why this exists
---------------
aiohttp 3.14 made `stream_writer` a required keyword-only argument of
`ClientResponse.__init__`. aioresponses builds its fake responses by calling
that constructor directly (`core.py`, `_build_response`) and does not pass it,
so every mocked request dies with:

    TypeError: ClientResponse.__init__() missing 1 required
               keyword-only argument: 'stream_writer'

No released aioresponses supports aiohttp 3.14 — 0.7.9 is the latest and fails
the same way. That left three options: stay on aiohttp 3.13.5 and carry eleven
known CVEs, drop aioresponses and rewrite the whole HTTP-mocking layer, or
supply the argument the new signature wants. This is the third.

The shim is deliberately in test code, not in production and not as a patch to
the installed package. Nothing about the application changes; only the fake
responses the suite builds do. It is also self-deleting by design: the
`hasattr` guard makes it a no-op the day aioresponses ships a version that
passes `stream_writer` itself, so nobody has to remember to remove it.

A plain `None` is not enough: aioresponses also passes `writer=None`, which
aiohttp reads as "request already sent" and makes it reach for
`stream_writer.output_size`. So the stub carries that one attribute and
nothing else. A mocked response never streams — aioresponses sets the body
directly rather than writing it through a connection — so zero bytes written
is the honest value, not a placeholder.

Second job: no test reaches a real endpoint
--------------------------------------------
main.py calls load_dotenv() when it is imported, and nearly every test module
imports it. On a developer machine that copies the real .env into the process
before the first test runs, and any test that drives a full round then posts to
whatever it names. Measured: one run sent 13 heartbeat pings to
the HEARTBEAT_URL in the real .env, which an external watchdog reads as 13
proofs of life from a monitor that may be dead.

The three channel variables are therefore set to the EMPTY STRING, not removed:
load_dotenv(override=False) fills a variable that is absent and skips one that
is present, empty or not, so deleting them would let the next import put them
straight back. Empty means "unconfigured" everywhere they are read (`or None`,
`if not url`). A test that needs a channel sets it itself.
"""

from __future__ import annotations

import inspect
import os
import subprocess

import aioresponses.core
import pytest
from aiohttp import ClientResponse

import targetstore

_CHANNEL_VARS = ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "HEARTBEAT_URL")

# Done here, at import, and not only in the fixture below: pytest loads this
# file before it collects the test modules, and collecting them is what runs
# main.py's load_dotenv(). Blanking first means the real values never enter the
# process at all, not even for the instant before the first test.
for _var in _CHANNEL_VARS:
    os.environ[_var] = ""

_needs_stream_writer = (
    "stream_writer" in inspect.signature(ClientResponse.__init__).parameters
)


class _NullStreamWriter:
    """The whole surface aiohttp touches on an already-sent mocked request."""

    output_size = 0


class _CompatClientResponse(ClientResponse):
    """ClientResponse that tolerates the argument aioresponses does not send."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("stream_writer", _NullStreamWriter())
        super().__init__(*args, **kwargs)


if _needs_stream_writer:
    # aioresponses resolves its default response class from this module-level
    # name, so rebinding it is enough — no subclassing of the mocker itself.
    aioresponses.core.ClientResponse = _CompatClientResponse


@pytest.fixture(autouse=True)
def _no_real_channels(monkeypatch):
    """
    Blank the webhook and heartbeat variables for every test.

    The import-time blanking above covers the session; this covers each test
    individually, so one that sets a URL and forgets to restore it, or that
    loads a .env of its own, cannot hand the next test a live channel. Through
    monkeypatch, so the value a test sets is undone with it.
    """
    for var in _CHANNEL_VARS:
        monkeypatch.setenv(var, "")


@pytest.fixture(autouse=True, scope="session")
def _children_start_with_blank_channels():
    """
    Hand every child process the channel variables blank, never absent.

    Blanking the parent does not close this gap. A test that runs `python
    main.py` builds the child's environment itself, and the obvious way is to
    copy os.environ MINUS the channel variables so the child starts clean.
    Absent is exactly what lets the child's own load_dotenv() fill them from the
    real .env: measured, 4 requests from three such tests with the parent
    already blank. So any env handed to a subprocess gets the variables it lacks
    as empty strings, and one that names a URL keeps it.

    Session-scoped, not per-test: a module-scoped fixture that spawns the child
    (test_alert_kinds does) is set up before any function-scoped fixture, so a
    per-test patch would not yet exist when it ran -- measured, 2 requests got
    through that way.
    """
    real_init = subprocess.Popen.__init__

    def init_with_blank_channels(self, *args, env=None, **kwargs):
        if env is not None:
            env = dict(env)
            for var in _CHANNEL_VARS:
                env.setdefault(var, "")
        real_init(self, *args, env=env, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess.Popen, "__init__", init_with_blank_channels)
        yield


@pytest.fixture(autouse=True)
def _isolate_target_store(tmp_path_factory, monkeypatch):
    """
    Point the dashboard-managed target store somewhere harmless, for every test.

    load_targets() prefers data/targets.json over the YAML it is handed. That
    is correct in production and poison in a test suite: whether a test passes
    would depend on whether a file happens to exist beside the process, so the
    suite would go green on a clean checkout and red on the developer machine
    that used the dashboard once.

    Autouse, and pointing at a path that does not exist, so the default answer
    is always "no store, use the YAML". A test that wants the store writes to
    it explicitly.

    The seed gets the same treatment. GET /api/targets now reads targets.yaml
    when there is no store, so the file beside the process -- the repo's real
    one, under pytest -- would decide what an API test sees. A nonexistent
    default means "the seed cannot be shown"; a test that wants one writes it
    and points SEED_FILE at it. main.TARGETS_FILE is bound at import and is not
    moved by this; a test that needs the monitor's default path patches it, as
    test_integration_config does.
    """
    scratch = tmp_path_factory.mktemp("store")
    monkeypatch.setattr(targetstore, "STORE_FILE", scratch / "targets.json")
    monkeypatch.setattr(targetstore, "SEED_FILE", scratch / "seed" / "targets.yaml")
