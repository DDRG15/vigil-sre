"""
api.py — read-only JSON endpoint over vigil-sre's state and history (the design note).

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
the design note dropped WAL with a condition attached: revisit when a real concurrent
reader exists. This is that reader, so the design note measured instead of inheriting.
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

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dashboard import render_page, render_rows
from history import HISTORY_DB_FILE, latency_percentiles, uptime_pct

logger = logging.getLogger("sre.api")

# Loopback by default, and it takes a deliberate act to change it. Publishing
# the operational map — which endpoints exist, when they fail, with what error
# — is a decision, not something an operator should back into by accident.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8787

STATE_FILE: Path = Path("state.json")

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
    simply old — the degraded-and-silent failure this phase must not have.
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

    Reuses an earlier release's aggregates verbatim rather than re-deriving them. Writing
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
        # The page loads no external resource — every style and script is
        # inline and self-authored, so the policy can be this tight. It also
        # means a value that somehow escaped _row()'s escaping still could not
        # pull in anything from outside.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def _dashboard_data(self) -> tuple[dict, dict, float | None]:
        """State, history aggregates and staleness — what both HTML routes need."""
        targets = read_state(self.state_path)
        history = history_payload(
            self.history_path, DEFAULT_WINDOW, list(targets)
        )["targets"]
        return targets, history, stale_seconds(targets)

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

            if route == "/api/status":
                targets = read_state(self.state_path)
                stale   = stale_seconds(targets)
                self._log_metric("status")
                if stale is not None:
                    # The one metric the design note named as the one that
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
    SQLite connection — the pattern an earlier release's aggregates already use — so no
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
