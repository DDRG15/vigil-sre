"""
api.py — read-only JSON endpoint over vigil-sre's state and history.

Why a separate process, and not a thread inside main.py
---------------------------------------------------------
main.py is not a long-lived service. It runs one probe cycle and exits — the
Dockerfile's CMD is a plain `python main.py`, and the compose loop restarts it
every 60 seconds. An HTTP server started inside that process would die with it
every minute. There was no alternative to weigh.

The welcome consequence is that probe isolation comes for free: this process
cannot slow down, block, or crash a run, because it is not in one. That is
usually a discipline to maintain; here it is a property of the deployment.

Read-only is enforced by the driver, not by intent
----------------------------------------------------
Every connection opens with sqlite3's URI `mode=ro`, so a write is physically
impossible rather than merely unintended — an INSERT fails with "attempt to
write a readonly database". This process runs alongside a live writer, and
turning "should not write" into "cannot write" removes any path by which the
reader could corrupt the writer's history.

Why no WAL, measured rather than assumed
------------------------------------------
An earlier revision dropped WAL with a condition attached: revisit when a
real concurrent reader exists. This is that reader, so the question was
measured here instead of inherited.
On 43,200 rows — 30 real days at one run per minute across six targets — the
writer holds its lock 203 ms once every 60,000 ms (a 0.34% duty cycle), and
the most expensive query here costs under 1 ms. A poll has roughly a 0.34%
chance of landing inside a write, and when it does it waits 203 ms and answers
anyway. WAL would buy latency nobody is paying, in exchange for the -wal/-shm
sidecars and a directory mount. Revisit if the run cadence drops below ~5 s.

Python  : 3.11+
Depends : stdlib only (http.server, sqlite3, json).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dashboard
import targetstore
from diagnostics import in_maintenance
from dashboard import render_page, render_rows
from history import (
    HISTORY_DB_FILE,
    latency_percentiles,
    status_strip,
    uptime_pct,
)

logger = logging.getLogger("sre.api")

# Loopback by default, and it takes a deliberate act to change it. Publishing
# the operational map — which endpoints exist, when they fail, with what error
# — is a decision, not something an operator should back into by accident.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8787

STATE_FILE: Path = Path("state.json")

#: Shared secret required to CHANGE the target list. Reads stay open.
#:
#: Adding a target is not editing a row in a list — it makes this monitor
#: fetch that URL from inside your network, on a schedule, and renders the
#: result on a page. That is the interesting capability here, and it is worth
#: a token even on loopback.
WRITE_TOKEN_VAR: str = "API_WRITE_TOKEN"

#: Maximum body this endpoint will read. A list of 200 targets is a few tens
#: of KB; anything larger is not a mistake worth accommodating.
MAX_BODY_BYTES: int = 256 * 1024


def write_token() -> str | None:
    return os.getenv(WRITE_TOKEN_VAR) or None

#: Windows the history endpoint accepts, mapped to days.
WINDOWS: dict[str, int] = {"1d": 1, "7d": 7, "30d": 30}
DEFAULT_WINDOW: str = "7d"


def read_state(state_path: Path) -> dict:
    """
    Return state.json's target map, or an empty one if it does not exist yet.

    A missing state file is a real, ordinary sequence — a freshly deployed
    instance before its first run — and answering 200 with nothing is the
    honest response. A 500 would say "something is broken" about a system
    that is merely new.
    """
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read '%s': %s", state_path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    # Mirrors StateManager._load_sync exactly, including its fallback: v2+
    # nests targets under a key, and a MISSING key means no targets — not
    # "treat the wrapper as a target". Falling back to `raw` here (as an
    # earlier version did) served schema_version itself as if it were a
    # monitored URL, which a dashboard iterating the map would then try to
    # read a status off. Legacy v1 WAS the flat mapping.
    if "schema_version" in raw:
        return raw.get("targets", {})
    return raw


def stale_seconds(targets: dict, now: datetime | None = None) -> float | None:
    """
    Age of the OLDEST last_checked being served, in seconds.

    This is the number that separates "the dashboard is alive" from "the
    dashboard is showing the past". If the probe process dies, this endpoint
    keeps answering 200 with data that looks perfectly well-formed and is
    simply old — the degraded-and-silent failure this must not have.
    Returns None when there is nothing to age.
    """
    now = now or datetime.now(timezone.utc)
    ages = []
    for record in targets.values():
        stamp = record.get("last_checked") if isinstance(record, dict) else None
        if not stamp:
            continue
        try:
            seen = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        ages.append((now - seen).total_seconds())
    return max(ages) if ages else None


def history_payload(db_path: Path, window: str, targets: list[str]) -> dict:
    """
    Uptime and TTFB percentiles per target over *window*.

    Reuses the --report aggregates verbatim rather than re-deriving them. Writing
    the same query twice is exactly how a CLI and an API end up reporting
    different numbers for the same data, and the test suite asserts the two
    agree rather than asserting each agrees with itself.
    """
    days  = WINDOWS[window]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    def _round(value: float | None, places: int) -> float | None:
        # Raw float noise (203.00000021234155) is not more precise, it is just
        # louder — the underlying measurement is a millisecond timing, not a
        # 14-digit quantity. Rounding at the serialisation boundary keeps the
        # stored value intact while giving consumers something a dashboard can
        # render without deciding for itself where to cut.
        return None if value is None else round(value, places)

    payload: dict[str, dict] = {}
    for url in targets:
        percentiles = latency_percentiles(db_path, url, since)
        payload[url] = {
            "uptime_pct" : _round(uptime_pct(db_path, url, since), 2),
            "p50_ttfb_ms": _round(percentiles["p50_ttfb_ms"] if percentiles else None, 1),
            "p95_ttfb_ms": _round(percentiles["p95_ttfb_ms"] if percentiles else None, 1),
        }
    return {"window": window, "targets": payload}


class _Handler(BaseHTTPRequestHandler):
    """One request. Reads two files, writes none."""

    #: Without this, socketserver.StreamRequestHandler.setup() never calls
    #: settimeout(), so a read on this socket can block forever. A client that
    #: announces a body and never sends one holds a thread indefinitely, and
    #: ThreadingHTTPServer caps nothing -- textbook Slowloris against the
    #: process that serves the dashboard and both read-only endpoints.
    timeout: float = 30.0

    # Injected by serve() so tests never depend on the process's cwd.
    state_path  : Path = STATE_FILE
    history_path: Path = HISTORY_DB_FILE

    server_version = "vigil-sre-api"
    sys_version    = ""          # do not advertise the Python version

    def _respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _respond_html(self, status: int, markup: str) -> None:
        encoded = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # The page loads no external resource, so the policy can forbid every
        # origin outright. The inline blocks are admitted by their SHA-256
        # hash, not by 'unsafe-inline': the earlier policy carried
        # `script-src 'unsafe-inline'`, which admits ANY inline script — an
        # injected one included — so it read as a defence against XSS while
        # providing none. base-uri and form-action are named explicitly
        # because neither inherits from default-src under CSP3, and an
        # injected <base> would redirect the page's own poll to another origin.
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; style-src {dashboard.STYLE_HASH}; "
            f"script-src {dashboard.SCRIPT_HASH}; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def _dashboard_data(self) -> tuple[dict, dict, float | None, dict, dict]:
        """State, aggregates, staleness and per-target strips — what both HTML
        routes need.

        The strips cover DEFAULT_WINDOW, the same window as the numbers beside
        them. A row whose uptime says "7 days" next to a strip covering one is
        two different claims about the same target, and the reader has no way
        to tell which one they are looking at.
        """
        targets = read_state(self.state_path)
        urls    = list(targets)
        history = history_payload(
            self.history_path, DEFAULT_WINDOW, urls
        )["targets"]
        since   = (
            datetime.now(timezone.utc) - timedelta(days=WINDOWS[DEFAULT_WINDOW])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        strips  = status_strip(self.history_path, urls, since)

        # Which targets are currently silenced, computed HERE rather than
        # stored: a window is a schedule, not a state, so asking "is it in
        # effect right now" at render time is the only answer that cannot go
        # stale between the probe writing and the page being read.
        now    = datetime.now(timezone.utc)
        stored = targetstore.read_store() or []
        muted  = {
            entry["url"]: window
            for entry in stored
            if (window := in_maintenance(entry.get("maintenance"), now)) is not None
        }
        return targets, history, stale_seconds(targets), strips, muted

    def _authorised(self) -> bool:
        """
        True when this request carries the write token.

        Fails CLOSED when no token is configured: without one there is no way
        to tell an operator from anything else that can reach the port, so the
        honest answer is to refuse writes rather than to accept everything.
        Reads are unaffected — they were already open.
        """
        expected = write_token()
        if not expected:
            logger.error(
                "Rechazando escritura: %s no está configurado. Poné un valor "
                "en .env para poder administrar targets desde el dashboard.",
                WRITE_TOKEN_VAR,
            )
            return False
        supplied = self.headers.get("X-Vigil-Token", "")
        # compare_digest and not ==: string comparison short-circuits on the
        # first differing byte, which leaks the prefix through timing.
        return hmac.compare_digest(supplied, expected)

    def _read_json_body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length no es un número.") from exc
        # Negative FIRST, and not folded into the comparison below: `-1 >
        # MAX_BODY_BYTES` is False, so a negative length sails past the size
        # guard and reaches read(-1), which means "read until EOF" -- the size
        # limit removed by the very check meant to enforce it.
        if length < 0:
            raise ValueError("Content-Length no puede ser negativo.")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"Cuerpo demasiado grande (máximo {MAX_BODY_BYTES} bytes).")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            # RecursionError is NOT a JSONDecodeError. Deeply nested JSON --
            # a few KB of "[[[[" is enough, far under MAX_BODY_BYTES -- raised
            # it straight out of do_GET/do_PUT, killing the handler thread and
            # resetting the client's connection. Reachable WITHOUT a token,
            # because the body is read before the auth check by design.
            raise ValueError(f"JSON inválido o demasiado anidado: {exc}") from exc

    def do_PUT(self) -> None:  # noqa: N802 — http.server's required spelling
        """Replace the whole target list. Whole-list, not per-target patches:
        two dashboards editing at once would otherwise interleave into a state
        neither of them asked for, and the list is small enough that sending
        all of it is cheaper than reconciling parts of it."""
        route = urlparse(self.path).path.rstrip("/") or "/"
        if route != "/api/targets":
            self._log_metric(route, error="not_found")
            self._respond(404, {"error": "not found", "path": route})
            return

        # The body is drained BEFORE the auth check, and that ordering is not
        # cosmetic: http.server keeps the connection alive, so replying 401
        # without reading what the client is still sending resets the socket.
        # The caller then sees a network error instead of a refusal — the
        # dashboard would report "sin conexión" for a wrong token. Reading is
        # safe because MAX_BODY_BYTES bounds it before any parsing happens.
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._log_metric("targets", error="invalid")
            self._respond(400, {"error": str(exc)})
            return

        if not self._authorised():
            self._log_metric("targets", error="unauthorised")
            self._respond(401, {
                "error": "escritura no autorizada",
                "hint" : f"Enviá la cabecera X-Vigil-Token con el valor de {WRITE_TOKEN_VAR}.",
            })
            return

        try:
            entries = body.get("targets") if isinstance(body, dict) else body
            stored  = targetstore.write_store(entries)
        except (ValueError, targetstore.ValidationError) as exc:
            # 400 with the reason, not a traceback: the message is meant to be
            # rendered next to the field the operator just typed.
            self._log_metric("targets", error="invalid")
            self._respond(400, {"error": str(exc)})
            return
        except OSError as exc:
            logger.error("No se pudo guardar la lista de targets: %s", exc)
            self._log_metric("targets", error=type(exc).__name__)
            self._respond(500, {"error": "no se pudo guardar la lista"})
            return

        self._log_metric("targets_write")
        self._respond(200, {"targets": stored})

    def do_GET(self) -> None:  # noqa: N802 — http.server's required spelling
        parsed = urlparse(self.path)
        route  = parsed.path.rstrip("/") or "/"

        try:
            if route == "/":
                self._log_metric("dashboard")
                self._respond_html(200, render_page(*self._dashboard_data()))
                return

            if route == "/partial/targets":
                self._log_metric("partial")
                self._respond_html(200, render_rows(*self._dashboard_data()))
                return

            if route == "/api/targets":
                self._log_metric("targets")
                stored = targetstore.read_store()
                self._respond(200, {
                    "targets": stored if stored is not None else [],
                    # The dashboard needs to say which file is authoritative:
                    # two files that both look canonical is worse than either.
                    "managed": stored is not None,
                    "writable": write_token() is not None,
                })
                return

            if route == "/api/status":
                targets = read_state(self.state_path)
                stale   = stale_seconds(targets)
                self._log_metric("status")
                if stale is not None:
                    # The one metric here that actually matters: returning it
                    # matters. Returning it only in the body would leave it
                    # visible to whoever happens to look and invisible to
                    # every alerting pipeline — and "nobody is looking" is
                    # precisely the condition it exists to detect.
                    logger.info(
                        "event_type=metric metric=api_stale_state_seconds "
                        "endpoint=status value=%.1f",
                        stale,
                    )
                self._respond(200, {
                    "targets"      : targets,
                    "stale_seconds": stale,
                })
                return

            if route == "/api/history":
                window = parse_qs(parsed.query).get("window", [DEFAULT_WINDOW])[0]
                if window not in WINDOWS:
                    self._log_metric("history", error="bad_window")
                    self._respond(400, {
                        "error"   : f"unknown window {window!r}",
                        "accepted": sorted(WINDOWS),
                    })
                    return
                targets = list(read_state(self.state_path))
                self._log_metric("history")
                self._respond(200, history_payload(self.history_path, window, targets))
                return

            self._log_metric(route, error="not_found")
            self._respond(404, {"error": "not found", "path": route})

        except sqlite3.Error as exc:
            # A JSON body, not a traceback: this is an operator-facing surface
            # and a stack trace on the wire tells a reader more about the
            # process than it tells them about their own outage.
            logger.error("Query failed for %s: %s", route, exc)
            self._log_metric(route, error=type(exc).__name__)
            self._respond(500, {"error": "could not read monitoring data"})

    def _log_metric(self, endpoint: str, error: str | None = None) -> None:
        if error is None:
            logger.info(
                "event_type=metric metric=api_requests_total endpoint=%s value=1",
                endpoint,
            )
        else:
            logger.warning(
                "event_type=metric metric=api_errors_total endpoint=%s "
                "reason=%s value=1",
                endpoint, error,
            )

    def log_message(self, fmt: str, *args) -> None:
        """Route http.server's own access log through our logger, not stderr."""
        logger.info("%s - %s", self.address_string(), fmt % args)


def serve(
    host        : str  = DEFAULT_HOST,
    port        : int  = DEFAULT_PORT,
    state_path  : Path = STATE_FILE,
    history_path: Path = HISTORY_DB_FILE,
) -> ThreadingHTTPServer:
    """
    Build the server. The caller decides whether to serve_forever().

    Threading is safe here because every request opens and closes its own
    SQLite connection — the pattern the --report aggregates already use — so no
    connection is ever shared across threads.
    """
    handler = type("_BoundHandler", (_Handler,), {
        "state_path": state_path, "history_path": history_path,
    })
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    host = os.getenv("API_HOST", DEFAULT_HOST)
    port = int(os.getenv("API_PORT", str(DEFAULT_PORT)))
    if host != DEFAULT_HOST:
        logger.warning(
            "Binding to %s, not %s — this endpoint has NO authentication, and "
            "off-loopback it publishes which targets exist, when they fail, "
            "and with what error. Put authentication in front of it.",
            host, DEFAULT_HOST,
        )
    server = serve(
        host, port,
        state_path=Path(os.getenv("STATE_FILE_PATH", str(STATE_FILE))),
    )
    logger.info("vigil-sre read-only API listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        server.shutdown()
        sys.exit(0)
