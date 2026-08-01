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
"""

from __future__ import annotations

import inspect

import aioresponses.core
import pytest
from aiohttp import ClientResponse

import targetstore

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
    """
    monkeypatch.setattr(
        targetstore, "STORE_FILE",
        tmp_path_factory.mktemp("store") / "targets.json",
    )
