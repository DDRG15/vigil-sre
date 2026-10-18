"""
tests/test_probe_body_cap.py -- one endpoint's response body must not be able to
take the monitor down.

_probe_once did `await resp.read()`: the whole body, decompressed, into memory,
with no limit. The 5 s request timeout bounds the time, not the size -- on a
fast path 5 s is hundreds of megabytes -- so one endpoint that starts returning
a huge body (a misrouted download, a log tail, a decompression bomb behind
`Content-Encoding: gzip`) exhausts the container's memory. The OOM killer takes
the whole process: every other target goes unprobed that minute, nothing is
persisted, no alert for the endpoint that caused it, no heartbeat.

The body is now read in chunks and counted as it arrives, against
MAX_BODY_BYTES, on the bytes delivered AFTER decompression. Content-Length is
not consulted: it is absent on chunked and close-delimited responses, and it
describes the compressed size on a gzip one. Over the cap is a probe failure for
that target alone, with an error that names the limit -- never a partial read
reported as UP.

Coverage:
  A. Under / at / over the cap, with and without Content-Length, and gzip
  B. expect_substring across chunk boundaries without holding the body
  C. Timings and byte counts stay correct
  D. Memory stays flat, and sibling targets are unaffected
"""

from __future__ import annotations

import asyncio
import gzip
import http.server
import os
import sys
import threading
import tracemalloc
from unittest.mock import AsyncMock

import aiohttp
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from diagnostics import BANDWIDTH_MIN_SAMPLE_BYTES
from main import Target

CAP = main.MAX_BODY_BYTES


# -----------------------------------------------------------------------------
# A local server. Loopback only: no test here reaches a real network.
# -----------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    """
    /len/<n>     n bytes with Content-Length
    /nolen/<n>   n bytes, no Content-Length (HTTP/1.0: the close ends the body)
    /gzip/<n>    n zero bytes, gzip-encoded: tiny on the wire, n once decoded
    /needle/<n>  n bytes with b"NEEDLE" as the last six
    """

    def _body(self, kind: str, n: int) -> tuple[bytes | None, dict[str, str]]:
        if kind == "gzip":
            return gzip.compress(b"\0" * n), {"Content-Encoding": "gzip"}
        return None, {}

    def do_GET(self) -> None:
        kind, _, raw = self.path.strip("/").partition("/")
        n = int(raw)
        payload, extra = self._body(kind, n)
        try:
            self.send_response(200)
            for key, value in extra.items():
                self.send_header(key, value)
            if payload is None:
                # Streamed in pieces, never built whole: the server shares this
                # process with the memory measurement below, and a 4 MiB bytes
                # object on its side would be counted against the probe.
                if kind in ("len", "needle"):
                    self.send_header("Content-Length", str(n))
                self.end_headers()
                fill   = b"a" if kind == "needle" else b"z"
                body_n = n - 6 if kind == "needle" else n
                sent = 0
                while sent < body_n:
                    step = min(65536, body_n - sent)
                    self.wfile.write(fill * step)
                    sent += step
                if kind == "needle":
                    self.wfile.write(b"NEEDLE")
                return
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client stopped reading at the cap: exactly the point

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


async def _probe(url: str, **kw):
    async with aiohttp.ClientSession() as session:
        return await main._probe_once(session, url, **kw)


# =============================================================================
# A. Under / at / over the cap
# =============================================================================


def test_the_cap_clears_the_bandwidth_sample_threshold() -> None:
    """Below 256 KB no window-limited verdict may fire (see diagnostics); a cap
    under that would silently turn the bandwidth rules off."""
    assert CAP >= BANDWIDTH_MIN_SAMPLE_BYTES


@pytest.mark.parametrize("kind", ["len", "nolen"])
@pytest.mark.parametrize("delta", [-1, 0])
async def test_a_body_at_or_under_the_cap_is_read_in_full(base, kind, delta) -> None:
    phases = await _probe(f"{base}/{kind}/{CAP + delta}")

    assert phases.body_bytes == CAP + delta
    assert phases.http_status == 200


@pytest.mark.parametrize("kind", ["len", "nolen"])
async def test_a_body_one_byte_over_the_cap_is_a_probe_failure(base, kind) -> None:
    """With and without Content-Length. Before the fix this returned phases with
    body_bytes = CAP + 1: a 'successful' probe that had read it all."""
    with pytest.raises(main._ProbeFailure) as failure:
        await _probe(f"{base}/{kind}/{CAP + 1}")

    detail = failure.value.detail
    assert "exceeded" in detail and str(CAP) in detail and "bytes" in detail, detail


async def test_a_huge_body_is_a_probe_failure_and_not_a_partial_read(base) -> None:
    with pytest.raises(main._ProbeFailure):
        await _probe(f"{base}/len/{CAP * 16}")


async def test_the_cap_counts_decompressed_bytes_not_the_wire_size(base) -> None:
    """A gzip body of CAP + 1 zeros is a few KB on the wire. Content-Length says
    so; the bytes delivered say otherwise, and only the second is memory."""
    assert len(gzip.compress(b"\0" * (CAP + 1))) < CAP // 100

    with pytest.raises(main._ProbeFailure) as failure:
        await _probe(f"{base}/gzip/{CAP + 1}")
    assert "exceeded" in failure.value.detail

    phases = await _probe(f"{base}/gzip/{CAP}")
    assert phases.body_bytes == CAP


# =============================================================================
# B. expect_substring across chunk boundaries
# =============================================================================


class _FakeContent:
    def __init__(self, chunks: list[bytes], pause_s: float = 0.0) -> None:
        self._chunks  = chunks
        self._pause_s = pause_s

    def iter_chunked(self, size: int):
        async def gen():
            for chunk in self._chunks:
                if self._pause_s:
                    await asyncio.sleep(self._pause_s)
                yield chunk
        return gen()


class _FakeResponse:
    status  = 200
    history = ()
    connection = None

    def __init__(self, chunks: list[bytes], pause_s: float = 0.0) -> None:
        self._chunks  = chunks
        self._pause_s = pause_s
        self.content  = _FakeContent(chunks, pause_s)

    async def read(self) -> bytes:
        """The one-shot read the probe used to do. Kept so the substring and
        timing tests describe the property (the needle is found wherever the
        chunks fall, the transfer is timed across the whole body), not the API
        the implementation happens to call."""
        for _ in self._chunks:
            await asyncio.sleep(self._pause_s)
        return b"".join(self._chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    def __init__(self, chunks: list[bytes], pause_s: float = 0.0) -> None:
        self._chunks  = chunks
        self._pause_s = pause_s

    def get(self, *a, **k) -> _FakeResponse:
        return _FakeResponse(self._chunks, self._pause_s)


async def _probe_chunks(chunks: list[bytes], pause_s: float = 0.0, **kw):
    return await main._probe_once(
        _FakeSession(chunks, pause_s), "https://x.example", **kw)


async def test_a_substring_split_across_two_chunks_is_found() -> None:
    phases = await _probe_chunks([b"xxxxNEE", b"DLExxxx"], expect_substring="NEEDLE")
    assert phases.body_bytes == 14


async def test_a_substring_split_across_every_byte_is_found() -> None:
    chunks = [bytes([b]) for b in b"..NEEDLE.."]
    await _probe_chunks(chunks, expect_substring="NEEDLE")


async def test_a_substring_that_ends_the_body_across_the_last_boundary_is_found() -> None:
    await _probe_chunks([b"a" * 1000, b"NEED", b"LE"], expect_substring="NEEDLE")


async def test_a_multibyte_character_split_mid_sequence_is_found() -> None:
    encoded = "café ok".encode("utf-8")
    cut = encoded.index(b"\xc3") + 1                 # between the two bytes of e-acute
    await _probe_chunks([encoded[:cut], encoded[cut:]], expect_substring="café")


async def test_a_one_byte_substring_needs_no_overlap() -> None:
    await _probe_chunks([b"abc", b"d"], expect_substring="d")


async def test_a_missing_substring_still_fails_and_is_not_invented_by_the_join() -> None:
    """The overlap must be real bytes of the previous chunk, not padding that
    could assemble the needle out of two chunks that do not contain it."""
    with pytest.raises(main._ProbeFailure, match="Content check failed"):
        await _probe_chunks([b"NEE", b"xDLE", b"NEED", b"xLE"], expect_substring="NEEDLE")


async def test_a_substring_before_the_cap_boundary_is_found_in_a_capped_size_body(base) -> None:
    phases = await _probe(f"{base}/needle/{CAP}", expect_substring="NEEDLE")
    assert phases.body_bytes == CAP


async def test_the_redacted_display_text_is_still_what_a_failed_check_reports() -> None:
    with pytest.raises(main._ProbeFailure) as failure:
        await _probe_chunks([b"nothing"], expect_substring="s3cret",
                            expect_substring_display="$TOKEN")
    assert "$TOKEN" in failure.value.detail and "s3cret" not in failure.value.detail


# =============================================================================
# C. Timings and byte counts
# =============================================================================


async def test_body_bytes_goodput_and_transfer_time_stay_correct() -> None:
    chunks = [b"a" * 70_000, b"b" * 70_000, b"c" * 1_000]
    phases = await _probe_chunks(chunks, pause_s=0.03)

    assert phases.body_bytes == 141_000
    assert phases.transfer_ms >= 80.0, "the transfer is timed across the whole read"
    assert phases.goodput_bps == pytest.approx(
        141_000 * 8.0 / (phases.transfer_ms / 1000.0))


async def test_an_empty_body_still_probes_ok_with_no_goodput() -> None:
    phases = await _probe_chunks([])
    assert phases.body_bytes == 0
    assert phases.goodput_bps is None


# =============================================================================
# D. Memory, and the other targets
# =============================================================================


async def test_reading_a_capped_body_does_not_hold_it_in_memory(base) -> None:
    """The point of reading incrementally: a CAP-sized body used to be CAP
    bytes resident (plus the joined copy). Streamed and discarded, the peak is a
    few chunks -- measured, not assumed."""
    tracemalloc.start()
    try:
        # The needle is the last six bytes, so the search has to carry its
        # overlap across the whole body before it finds anything.
        await _probe(f"{base}/needle/{CAP}", expect_substring="NEEDLE")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < CAP // 4, f"peak {peak} B for a {CAP} B body: the body was held"


async def test_one_oversized_target_does_not_affect_its_siblings(
        base, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(main, "dispatch_alert", AsyncMock(return_value=True))
    heartbeat = AsyncMock()
    monkeypatch.setattr(main, "_send_heartbeat", heartbeat)

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)

    big, ok = f"{base}/len/{CAP * 8}", f"{base}/len/1024"
    state_path = tmp_path / "state.json"
    report = main.RunReport()

    down = await main.run_health_checks(
        targets=[Target(url=big), Target(url=ok)],
        state_path=state_path, history_path=tmp_path / "h.db", report=report)

    state = main.StateManager(state_path)._state
    assert state[big]["status"] == main.STATUS_DOWN
    assert "exceeded" in state[big]["last_error"]
    # Performance can vary under load; this contract is successful probing,
    # not a latency guarantee. Verify the sibling's actual response and body.
    assert state[ok]["status"] in (main.STATUS_UP, main.STATUS_DEGRADED)
    assert state[ok]["diagnostics"]["measured"]["http_status"] == 200
    assert state[ok]["diagnostics"]["measured"]["body_bytes"] == 1024
    assert down == 1
    assert report.internal_errors == 0, "the oversize body crashed a check"
    assert heartbeat.await_count == 1, "the round completed"
