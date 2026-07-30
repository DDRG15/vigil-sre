"""
main.py — Senior SRE Infrastructure Health Checker (v3)

What's new in v3
-----------------
* Async/concurrent  : Replaced requests + sequential loop with asyncio +
                      aiohttp.  All health checks run concurrently via
                      asyncio.gather(), slashing total wall-clock time from
                      O(n * timeout) to O(1 * timeout).
* External config   : Target URLs are no longer hard-coded.  They are loaded
                      from targets.yaml at startup, making the script config-
                      driven and deployable without code changes.
* Graceful shutdown : SIGINT / SIGTERM are caught via asyncio's add_signal_handler.
                      In-flight tasks are awaited (not cancelled mid-flight)
                      before the event loop exits, preventing torn state writes.
* Async alerting    : Discord Webhook POSTs also use aiohttp so they don't
                      block the event loop.
* Async retries     : Replaced tenacity's sync decorator with a hand-rolled
                      async retry loop that honours asyncio.sleep() for
                      backoff — safe inside the event loop.
* Thread-safe state : StateManager gains an asyncio.Lock so concurrent
                      coroutines can't race on state.json writes.

Author  : Senior SRE
Python  : 3.11+

Usage:
    1. cp .env.example .env          →  fill in DISCORD_WEBHOOK_URL
    2. edit targets.yaml             →  add / remove URLs
    3. pip install -r requirements.txt
    4. python main.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TypedDict

import aiohttp
import yaml
from dotenv import load_dotenv

from diagnostics import (
    DEGRADED_TTFB_MS,
    RTT_HIGH_MS,
    ConnectionSample,
    ProbePhases,
    analyze,
    build_trace_config,
    cert_days_left,
    degraded_reason,
    host_port_from_url,
    phases_to_dict,
    sample_connection,
)
from history import (
    HISTORY_DB_FILE,
    CheckOutcome,
    HistoryRecorder,
    _retention_days_from_env,
    latency_percentiles,
    uptime_pct,
)
from notifiers import (
    AlertKind,
    dispatch_alert,
)

# ---------------------------------------------------------------------------
# Bootstrap — load .env before anything reads os.getenv()
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Logging — dual handler (console + rotating flat file)
# ---------------------------------------------------------------------------
LOG_FORMAT  = "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOG_MAX_BYTES   : int = 10 * 1024 * 1024  # rotate at 10 MiB
LOG_BACKUP_COUNT: int = 5                 # keep 5 rotated files (50 MiB total cap)

# On Windows the default console codec (cp1252) cannot encode the emoji
# characters used in log messages. Reconfigure stdout to UTF-8 so the
# StreamHandler never raises UnicodeEncodeError; fall back gracefully if the
# stream doesn't support reconfiguration (e.g. redirected pipes).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "health_checker.log", mode="a", maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("sre.health_checker")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT_S : int  = 5         # per-attempt HTTP timeout (seconds)
EXPECTED_STATUS   : int  = 200
RETRY_ATTEMPTS    : int  = 3         # total attempts (1 original + 2 retries)
RETRY_BACKOFF_BASE: float = 2.0      # exponential base: 2 s → 4 s → 8 s
RETRY_BACKOFF_MAX : float = 10.0     # cap on sleep between retries
TARGETS_FILE      : Path = Path("targets.yaml")
STATE_FILE        : Path = Path("state.json")

# Chrome 124 UA — passes basic WAF fingerprint checks
BROWSER_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_HEADERS: dict[str, str] = {
    "User-Agent"     : BROWSER_UA,
    "Accept"         : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

STATUS_UP      : str = "UP"
STATUS_DOWN    : str = "DOWN"
STATUS_DEGRADED: str = "DEGRADED"   # probe succeeded but a performance finding fired

# Flap hysteresis: consecutive failed RUNS required to knock a known-healthy
# (UP or DEGRADED) target into confirmed DOWN. Retries inside _probe_with_backoff
# already absorb blips within one run; this absorbs flapping ACROSS runs, where
# a target bouncing UP/DOWN every run would otherwise fire an alert every run.
# A target with no prior history (first-ever observation) skips the hysteresis
# and fails straight to DOWN — there is no flapping to protect against on a
# first sample, and smoke-test targets that are deliberately always-down must
# still alert on their very first run.
DOWN_CONFIRMATIONS: int = 2

STATE_SCHEMA_VERSION: int = 2   # bump whenever state.json's on-disk shape changes

# Webhook delivery retry now lives in notifiers.py alongside the Notifier
# base class that applies it — see WEBHOOK_RETRY_ATTEMPTS / _BASE_S /
# _AFTER_CAP_S there. It moved with the code it governs (the design note).

# If a full run takes longer than this, the docker-compose `sleep 60` loop
# (or an equivalent cron/systemd-timer interval) is scheduling overlapping
# runs without anyone knowing — two StateManager instances writing the same
# state.json concurrently is exactly the kind of silent corruption the
# asyncio.Lock protects against WITHIN one run, but not across two runs that
# were never supposed to overlap in the first place.
RUN_BUDGET_S: float = 60.0


# ---------------------------------------------------------------------------
# YAML target loader
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """
    One monitored endpoint as configured in targets.yaml.

    ``expect_substring`` is the content assertion: when set, a 200 response
    whose body does not contain it counts as a probe failure. A status-code
    check alone cannot tell a real response from a server returning 200 with
    an error page or an empty JSON body — the substring is how the probe
    tells "answered" from "answered correctly". The body is already read in
    full for the goodput measurement, so this assertion costs nothing extra.

    ``expect_substring`` may reference an environment variable instead of a
    literal (``${HEALTH_TOKEN}``, resolved from .env at load time) for values
    that are secrets rather than public fixtures. ``expect_substring_display``
    is what every downstream sink (logs, Discord, state.json, history.db)
    shows on a failed check: the literal value for a plain string (already
    public in git either way), or ``${VAR_NAME} (from env)`` for an env-var
    reference — never the resolved secret. Redacting once here, at load time,
    means the 5 sinks inherit it for free instead of needing 5 separate fixes.

    ``expected_status``, ``timeout_s``, ``degraded_ttfb_ms``, and
    ``degraded_rtt_ms`` are per-target overrides of the module-wide defaults
    (EXPECTED_STATUS, REQUEST_TIMEOUT_S, and diagnostics.DEGRADED_TTFB_MS /
    RTT_HIGH_MS). A single global RTT threshold produces chronic false
    DEGRADEDs for a target that is legitimately far away, and a global
    EXPECTED_STATUS makes a healthy 204-returning endpoint unmonitorable —
    a threshold with chronic false positives gets ignored, which defeats the
    monitor entirely. None means "use the module default".

    This dict-based YAML schema (string OR object per entry) is also the
    extension point for future per-target settings — auth headers, etc. —
    without another schema migration.
    """

    url                     : str
    expect_substring        : str   | None = None
    expect_substring_display: str   | None = None
    expected_status         : int   | None = None
    timeout_s               : float | None = None
    degraded_ttfb_ms        : float | None = None
    degraded_rtt_ms         : float | None = None


_ENV_VAR_REF     = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_VAR_SUSPECT = re.compile(r"\$\{")


def _resolve_expect_substring(raw: str, path: Path, url: str) -> tuple[str, str | None]:
    """
    Resolve a ${VAR_NAME} env-var reference in expect_substring, if present.

    Returns (value_to_match, value_to_display). A plain literal returns
    (raw, None) — None means "no override", so downstream code displays the
    value itself on a failed check (safe: targets.yaml is committed to git,
    nothing to protect). A ${VAR_NAME} reference is the signal that this
    value is a secret: it resolves to the real value from .env for matching,
    and to a safe placeholder for display. Exits at load time (like every
    other malformed-target check in this loader) on any of three malformed
    inputs — a probe that silently never matches, or never fails, because its
    secret didn't resolve as intended is a worse failure mode than refusing
    to start:

    1. A value containing "${" that isn't a clean, whole-string reference
       (concatenation like "Bearer ${TOKEN}", missing braces, stray spaces).
       This would otherwise be compared byte-for-byte against the response
       body, never match, and leave the target DOWN forever.
    2. A lowercase or mixed-case variable name. os.environ is
       case-insensitive on Windows and case-sensitive on Linux — ${token}
       against a TOKEN= entry resolves fine in local dev and hard-exits the
       moment the same targets.yaml runs in the container.
    3. A variable that resolves to None (unset) or "" (set but empty).
       "" is contained in every byte string, so an empty value would silently
       turn the content assertion into a no-op that reports UP over any body,
       including an error page.
    """
    match = _ENV_VAR_REF.match(raw)
    if match is None:
        if _ENV_VAR_SUSPECT.search(raw):
            logger.critical(
                "'%s' target %r has a malformed expect_substring (%r). An "
                "environment reference must be the ENTIRE value, e.g. "
                "expect_substring: ${HEALTH_TOKEN}. Concatenation is not "
                "supported.",
                path, url, raw,
            )
            sys.exit(1)
        return raw, None
    var_name = match.group(1)
    if var_name != var_name.upper():
        logger.critical(
            "'%s' target %r references ${%s}, but environment references "
            "must be uppercase — Windows resolves them case-insensitively "
            "and Linux does not, so this works in dev and hard-exits in the "
            "container. Use ${%s}.",
            path, url, var_name, var_name.upper(),
        )
        sys.exit(1)
    value = os.getenv(var_name)
    if not value:
        logger.critical(
            "'%s' target %r references ${%s} in expect_substring, but %s is "
            "%s. Add a non-empty value to .env and try again.",
            path, url, var_name, var_name,
            "not set" if value is None else "set to an empty string",
        )
        sys.exit(1)
    return value, f"${{{var_name}}} (from env)"


def _validated_numeric_field(
    entry: dict, key: str, path: Path, *,
    numeric_types: tuple[type, ...],
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> int | float | None:
    """
    Return entry[key] if absent, or a plain number of an allowed type within
    the allowed range; otherwise exit with a message naming the fix.

    ``bool`` is explicitly rejected even though it is a subtype of ``int`` in
    Python — YAML's ``true``/``false`` must never silently become 1/0 for a
    numeric override.

    Range matters as much as type. aiohttp reads ClientTimeout(total=0) as
    "no timeout at all", so a timeout_s of 0 turns a bounded probe into one
    that hangs forever on an unresponsive target — freezing the whole run's
    asyncio.gather and every other target with it. A threshold of 0 or less
    fires on every healthy probe, producing exactly the chronic false
    positives per-target overrides exist to eliminate.
    """
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numeric_types):
        logger.critical(
            "'%s' target %r has a non-numeric %s (%r). Remove any quotes so "
            "YAML parses it as a number, e.g.  %s: 10.",
            path, entry.get("url"), key, value, key,
        )
        sys.exit(1)
    if minimum is not None and value < minimum:
        logger.critical(
            "'%s' target %r has %s=%r, below the minimum of %s. Values at or "
            "below zero disable the protection this field configures instead "
            "of tightening it.",
            path, entry.get("url"), key, value, minimum,
        )
        sys.exit(1)
    if maximum is not None and value > maximum:
        logger.critical(
            "'%s' target %r has %s=%r, above the maximum of %s.",
            path, entry.get("url"), key, value, maximum,
        )
        sys.exit(1)
    return value


def load_targets(path: Path = TARGETS_FILE) -> list[Target]:
    """
    Parse *path* as YAML and return the list of Targets.

    Expected structure — plain URL strings and objects can be mixed::

        targets:
          - https://www.example.com
          - url: https://api.example.com/health
            expect_substring: '"status":"ok"'
            expected_status: 204
            timeout_s: 10
            degraded_ttfb_ms: 3000
            degraded_rtt_ms: 250
          - url: https://api.example.com/private-health
            expect_substring: ${HEALTH_TOKEN}   # resolved from .env, never logged

    Exits with a clear message if the file is missing, malformed, empty,
    contains an object entry without a ``url`` key, or references a
    ``${VAR_NAME}`` in ``expect_substring`` that isn't set in the environment.

    Args:
        path: Path to the YAML targets file.

    Returns:
        Non-empty list of Target records.
    """
    if not path.exists():
        logger.critical(
            "Targets file '%s' not found.  "
            "Create it from targets.yaml.example and try again.", path
        )
        sys.exit(1)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.critical("Failed to parse '%s': %s", path, exc)
        sys.exit(1)

    entries: list = raw.get("targets", []) if isinstance(raw, dict) else []

    targets: list[Target] = []
    for entry in entries:
        if isinstance(entry, str):
            targets.append(Target(url=entry))
        elif isinstance(entry, dict) and entry.get("url"):
            expect = entry.get("expect_substring")
            if expect is not None and not isinstance(expect, str):
                # YAML parses `expect_substring: 200` (no quotes) as an int.
                # _probe_once calls .encode("utf-8") on this value — an
                # unvalidated int would crash that target's probe every run,
                # with the traceback misattributed to a "bug" in vigil rather
                # than a config typo. Fail fast at load time instead.
                logger.critical(
                    "'%s' target %r has a non-string expect_substring (%r). "
                    "Quote it in YAML, e.g.  expect_substring: '200'.",
                    path, entry["url"], expect,
                )
                sys.exit(1)
            expect_display = None
            if expect is not None:
                expect, expect_display = _resolve_expect_substring(expect, path, str(entry["url"]))
            expected_status = _validated_numeric_field(
                entry, "expected_status", path, numeric_types=(int,),
                minimum=100, maximum=599,
            )
            timeout_s = _validated_numeric_field(
                entry, "timeout_s", path, numeric_types=(int, float),
                minimum=0.1,
            )
            degraded_ttfb_ms = _validated_numeric_field(
                entry, "degraded_ttfb_ms", path, numeric_types=(int, float),
                minimum=0.1,
            )
            degraded_rtt_ms = _validated_numeric_field(
                entry, "degraded_rtt_ms", path, numeric_types=(int, float),
                minimum=0.1,
            )
            if timeout_s is not None:
                # Not an abort: a legitimately slow endpoint is a valid use
                # case for a longer timeout_s, but the operator needs to see
                # what it costs -- a full outage on this target now stretches
                # the whole run past RUN_BUDGET_S, risking overlapping runs.
                worst_case_s = (
                    timeout_s * RETRY_ATTEMPTS
                    + RETRY_BACKOFF_BASE ** 1 + RETRY_BACKOFF_BASE ** 2
                )
                if worst_case_s > RUN_BUDGET_S:
                    logger.warning(
                        "'%s' target %r has timeout_s=%s: worst case %.0fs "
                        "(%d attempts + backoff) exceeds RUN_BUDGET_S=%.0fs, so "
                        "a full outage on this target stretches the whole run.",
                        path, entry["url"], timeout_s, worst_case_s,
                        RETRY_ATTEMPTS, RUN_BUDGET_S,
                    )
            targets.append(Target(
                url=str(entry["url"]),
                expect_substring=expect,
                expect_substring_display=expect_display,
                expected_status=expected_status,
                timeout_s=timeout_s,
                degraded_ttfb_ms=degraded_ttfb_ms,
                degraded_rtt_ms=degraded_rtt_ms,
            ))
        else:
            logger.critical(
                "'%s' contains an invalid target entry (%r). Each entry must "
                "be a URL string or an object with a 'url' key.", path, entry
            )
            sys.exit(1)

    if not targets:
        logger.critical(
            "'%s' contains no targets under the 'targets:' key.  "
            "Add at least one URL and retry.", path
        )
        sys.exit(1)

    logger.info("Loaded %d target(s) from '%s'.", len(targets), path)
    return targets


# ---------------------------------------------------------------------------
# State management  (async-safe via asyncio.Lock)
# ---------------------------------------------------------------------------

class UrlState(TypedDict, total=False):
    """
    Persisted state record for a single monitored URL.

    ``total=False`` because ``diagnostics`` is optional: it is present only
    after a successful probe that produced a latency/BDP breakdown, and
    absent for URLs that have only ever been DOWN. Every reader must treat
    it as possibly-missing.
    """

    status              : str    # "UP" | "DEGRADED" | "DOWN"   (always present)
    last_checked        : str    # ISO-8601 UTC — updated every run
    last_changed        : str    # ISO-8601 UTC — updated only on a transition
    last_error          : str    # "" when UP; failure detail when DOWN/pending; reason when DEGRADED
    diagnostics         : dict   # last latency/BDP breakdown; on UP and DEGRADED probes
    consecutive_failures: int    # failed runs in a row since last healthy state; absent/0 when healthy


class StateManager:
    """
    Async-safe, JSON-backed state store for monitored URL statuses.

    Concurrency model
    -----------------
    An asyncio.Lock serialises all writes.  This is sufficient because the
    entire program runs in a single OS thread (the event loop).  The lock
    prevents two coroutines from interleaving a read-modify-write cycle when
    asyncio.gather() runs checks concurrently.

    Atomic writes
    -------------
    State is written to ``<path>.tmp`` first and then renamed into place.
    A crash or SIGKILL between the two syscalls leaves the old state.json
    intact — it never produces a zero-byte or partial file.
    """

    def __init__(self, path: Path = STATE_FILE) -> None:
        self._path  = path
        self._lock  = asyncio.Lock()
        self._state : dict[str, UrlState] = self._load_sync()

    # ------------------------------------------------------------------ I/O

    def _load_sync(self) -> dict[str, UrlState]:
        """
        Synchronous load called once at construction (before the loop starts).

        Understands both on-disk shapes: schema v2 wraps the URL records
        under a ``targets`` key alongside ``schema_version``; the legacy v1
        file WAS the flat url→record mapping itself, with no wrapper at all.
        A v1 file is migrated transparently — this returns its content as the
        target map, and the very next write persists it in v2 shape. There is
        no separate migration step to run or forget.
        """
        if not self._path.exists():
            logger.info("No state file found at '%s' — starting fresh.", self._path)
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read '%s' (%s) — starting with empty state.", self._path, exc
            )
            return {}

        if not isinstance(raw, dict):
            logger.warning(
                "State file '%s' root is not a JSON object — starting fresh.", self._path
            )
            return {}

        if "schema_version" in raw:
            return raw.get("targets", {})

        logger.info(
            "State file '%s' has no schema_version — treating as legacy v1 and "
            "migrating to v%d on next write.", self._path, STATE_SCHEMA_VERSION,
        )
        return raw

    def _write_sync(self) -> None:
        """Atomic write: tmp file → rename.  Called inside the lock."""
        tmp = self._path.with_suffix(".tmp")
        document = {
            "schema_version": STATE_SCHEMA_VERSION,
            "targets"       : self._state,
        }
        try:
            tmp.write_text(
                json.dumps(document, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError as exc:
            logger.error("State write failed for '%s': %s", self._path, exc)

    # ------------------------------------------------------------------ Helpers

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------ Public API

    def current_status(self, url: str) -> str | None:
        """
        The status currently persisted for *url*, or None if never observed.

        A synchronous, lock-free dict read — safe here because it is only
        ever called immediately after this same URL's own set_up/set_degraded/
        set_down call already completed under the lock, so no other coroutine
        can be mutating this specific key at the same instant. Used to report
        the real (hysteresis-aware) final status of a run, e.g. for the
        --strict exit code: a target still inside its DOWN_CONFIRMATIONS
        window reports its preserved healthy status, not DOWN, exactly as
        state.json itself does.
        """
        record = self._state.get(url)
        return record["status"] if record is not None else None

    async def set_up(self, url: str, diagnostics: dict | None = None) -> bool:
        """
        Mark *url* as UP.

        Args:
            url:         The monitored URL.
            diagnostics: Optional latency/BDP breakdown from diagnostics.
                         phases_to_dict(); persisted verbatim under the
                         ``diagnostics`` key when provided. None leaves any
                         previously stored breakdown untouched is NOT the
                         behaviour — a None simply omits the key on this write,
                         reflecting "measured UP but no breakdown captured".

        Returns:
            True  — first check, or previous state was DOWN  → send alert.
            False — was already UP                           → suppress alert.
        """
        async with self._lock:
            now      = self._utc_now()
            previous = self._state.get(url)
            changed  = previous is None or previous["status"] != STATUS_UP

            record = UrlState(
                status      =STATUS_UP,
                last_checked=now,
                last_changed=now if changed else previous["last_changed"],
                last_error  ="",
            )
            if diagnostics is not None:
                record["diagnostics"] = diagnostics

            self._state[url] = record
            self._write_sync()
            return changed

    async def set_down(self, url: str, error: str) -> bool:
        """
        Record a failed probe for *url*, subject to flap hysteresis.

        A target with no prior history fails straight to DOWN — there is no
        flapping to protect against on a first observation. A target coming
        from a healthy state (UP/DEGRADED) needs DOWN_CONFIRMATIONS
        consecutive failed runs before it is confirmed DOWN; until then its
        previous status is preserved (only last_checked, last_error, and the
        strike counter advance), so a single blip that a future run recovers
        from never reaches the alert path at all.

        Returns:
            True  — DOWN just confirmed (first-ever observation, or the
                    strike counter reached DOWN_CONFIRMATIONS)  → send alert.
            False — already confirmed DOWN, or hysteresis still pending
                    → suppress alert.
        """
        async with self._lock:
            now      = self._utc_now()
            previous = self._state.get(url)

            if previous is None:
                self._state[url] = UrlState(
                    status               =STATUS_DOWN,
                    last_checked         =now,
                    last_changed         =now,
                    last_error           =error,
                    consecutive_failures =1,
                )
                self._write_sync()
                return True

            if previous["status"] == STATUS_DOWN:
                self._state[url] = UrlState(
                    status               =STATUS_DOWN,
                    last_checked         =now,
                    last_changed         =previous["last_changed"],
                    last_error           =error,
                    consecutive_failures =previous.get("consecutive_failures", 1) + 1,
                )
                self._write_sync()
                return False

            strikes = previous.get("consecutive_failures", 0) + 1
            if strikes < DOWN_CONFIRMATIONS:
                # Hysteresis holds: keep the previous healthy status and its
                # diagnostics, only advance the failure bookkeeping.
                pending = dict(previous)
                pending["last_checked"]          = now
                pending["last_error"]            = error
                pending["consecutive_failures"]  = strikes
                self._state[url] = pending  # type: ignore[assignment]
                self._write_sync()
                return False

            self._state[url] = UrlState(
                status               =STATUS_DOWN,
                last_checked         =now,
                last_changed         =now,
                last_error           =error,
                consecutive_failures =strikes,
            )
            self._write_sync()
            return True

    async def set_degraded(
        self, url: str, reason: str, diagnostics: dict | None = None
    ) -> bool:
        """
        Mark *url* as DEGRADED — reachable and answering, but hurting.

        Args:
            url:         The monitored URL.
            reason:      Human-readable degradation reason (stored in
                         last_error and shown in the yellow alert).
            diagnostics: Optional latency/BDP breakdown, persisted verbatim.

        Returns:
            True  — first check, or previous state was UP/DOWN → send alert.
            False — was already DEGRADED                       → suppress alert.
        """
        async with self._lock:
            now      = self._utc_now()
            previous = self._state.get(url)
            changed  = previous is None or previous["status"] != STATUS_DEGRADED

            record = UrlState(
                status      =STATUS_DEGRADED,
                last_checked=now,
                last_changed=now if changed else previous["last_changed"],
                last_error  =reason,
            )
            if diagnostics is not None:
                record["diagnostics"] = diagnostics

            self._state[url] = record
            self._write_sync()
            return changed



# ---------------------------------------------------------------------------
# Async probe with manual exponential-backoff retry loop
# ---------------------------------------------------------------------------

class _ProbeFailure(Exception):
    """Raised by _probe_once() on any non-200 response or network error."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _extract_cert_days(resp: aiohttp.ClientResponse) -> int | None:
    """
    Pull the peer certificate off the live connection and return days-to-expiry.

    Returns None for plain HTTP, or when the transport does not expose an
    SSL object (connection reused from pool without re-exposing it, or a
    mocked transport under test). Never raises — a missing cert must not
    fail the probe.
    """
    try:
        connection = resp.connection
        if connection is None or connection.transport is None:
            return None
        ssl_object = connection.transport.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        return cert_days_left(ssl_object.getpeercert())
    except (AttributeError, OSError, ValueError):
        return None


async def _probe_once(
    session: aiohttp.ClientSession,
    url: str,
    sample: ConnectionSample | None = None,
    expect_substring: str | None = None,
    expected_status: int = EXPECTED_STATUS,
    timeout_s: float = REQUEST_TIMEOUT_S,
    expect_substring_display: str | None = None,
) -> ProbePhases:
    """
    Fire a single async HTTP GET against *url* and measure its phases.

    Beyond the pass/fail verdict, this reads the full response body so the
    transfer can be timed and turned into a goodput sample, and it captures
    per-phase timings (DNS, connect, TTFB, transfer) via the TraceConfig
    context dict. RTT, TLS handshake time, and ALPN/h2 come from the
    out-of-band ConnectionSample the caller took — measured on connections we
    control, which aiohttp's own timings cannot provide cleanly.

    Args:
        session:          Shared aiohttp.ClientSession (must carry the
                          diagnostics TraceConfig for DNS/connect timings).
        url:              Target URL.
        sample:           Pre-taken out-of-band connection sample, or None if
                          it failed.
        expect_substring: Optional content assertion. A 200 whose body does
                          not contain this string counts as a probe failure —
                          a status code alone cannot tell "answered" from
                          "answered correctly" (error page, empty JSON, etc).
        expected_status:  HTTP status that counts as success (default 200).
                          Per-target override for endpoints that legitimately
                          answer 204, 202, etc.
        timeout_s:        Per-attempt timeout in seconds (default REQUEST_TIMEOUT_S).
                          Per-target override for endpoints with a slower SLA.
        expect_substring_display: What a failed content check reports instead
                          of expect_substring itself — see Target's docstring.
                          Falls back to expect_substring when not given.

    Returns:
        A fully-populated ProbePhases on an `expected_status` response that
        also passes the content assertion (when one is configured).

    Raises:
        _ProbeFailure: On timeout, connection error, unexpected status, or a
                       failed content assertion.
    """
    rtt_ms = sample.rtt_ms if sample is not None else None
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    trace_ctx: dict = {}
    start = time.monotonic()
    redirected = False
    try:
        async with session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers=REQUEST_HEADERS,
            trace_request_ctx=trace_ctx,
        ) as resp:
            if resp.status != expected_status:
                raise _ProbeFailure(f"HTTP {resp.status} (expected {expected_status})")

            # On a redirect chain, the TraceConfig hooks overwrite dns_ms/
            # connect_total_ms on every hop and keep only the last one, but
            # ttfb_ms below spans the ENTIRE chain (measured from `start`,
            # before the first hop). Subtracting a single-hop dns+connect from
            # a multi-hop ttfb would attribute the earlier hops' full round
            # trip to "the server thinking" — server_processing_ms is voided
            # for redirected responses below instead of reporting a number
            # that isn't measuring what its name says it measures.
            redirected = len(resp.history) > 0

            ttfb_ms = (time.monotonic() - start) * 1000.0
            cert_days = _extract_cert_days(resp)

            transfer_start = time.monotonic()
            body = await resp.read()
            transfer_ms = (time.monotonic() - transfer_start) * 1000.0
            body_bytes = len(body)

            if expect_substring is not None and expect_substring.encode("utf-8") not in body:
                display = expect_substring_display if expect_substring_display is not None else expect_substring
                raise _ProbeFailure(
                    f"Content check failed: {display!r} not found in body"
                )

            logger.info(
                "  attempt OK — HTTP %s ← %s (%d bytes)", resp.status, url, body_bytes
            )

    except asyncio.TimeoutError:
        raise _ProbeFailure(f"Timeout (>{timeout_s}s)")
    except aiohttp.ClientConnectionError as exc:
        raise _ProbeFailure(f"ConnectionError: {exc}")
    except aiohttp.ClientError as exc:
        raise _ProbeFailure(f"ClientError: {exc}")

    dns_ms           = trace_ctx.get("dns_ms")
    connect_total_ms = trace_ctx.get("connect_total_ms")
    reused           = trace_ctx.get("connection_reused", False)

    # TLS handshake time, ALPN, and h2 support come from the out-of-band
    # sampler — measured on a connection it controls, back-to-back with its
    # own RTT sample. This removes the old derive-from-aiohttp-connect
    # subtraction, whose two figures came from different connections and could
    # disagree enough to produce a negative TLS time.
    tls_ms        = sample.tls_ms if sample is not None else None
    alpn_protocol = sample.alpn_protocol if sample is not None else None
    h2_supported  = sample.h2_supported if sample is not None else None

    # Server processing is the time-to-first-byte with the known network
    # setup (DNS + connect/TLS) and one request→first-byte round trip removed.
    # What remains is the server thinking. Clamp negatives to None. Never
    # computed for a redirected response — see the `redirected` comment above.
    server_processing_ms: float | None = None
    if rtt_ms is not None and not redirected:
        network_setup = (dns_ms or 0.0) + (connect_total_ms or 0.0) + rtt_ms
        remainder = ttfb_ms - network_setup
        server_processing_ms = remainder if remainder > 0 else None

    # Goodput needs a transfer long enough to out-resolve the clock; a body
    # that arrives in under a millisecond yields a meaningless division.
    goodput_bps: float | None = None
    if body_bytes > 0 and transfer_ms >= 1.0:
        goodput_bps = (body_bytes * 8.0) / (transfer_ms / 1000.0)

    return ProbePhases(
        url=url,
        http_status=resp.status,
        ttfb_ms=ttfb_ms,
        transfer_ms=transfer_ms,
        body_bytes=body_bytes,
        connection_reused=reused,
        dns_ms=dns_ms,
        connect_total_ms=connect_total_ms,
        rtt_ms=rtt_ms,
        tls_ms=tls_ms,
        server_processing_ms=server_processing_ms,
        goodput_bps=goodput_bps,
        tls_cert_days_left=cert_days,
        alpn_protocol=alpn_protocol,
        h2_supported=h2_supported,
    )


async def _probe_with_backoff(
    session: aiohttp.ClientSession,
    url: str,
    sample: ConnectionSample | None = None,
    expect_substring: str | None = None,
    expected_status: int = EXPECTED_STATUS,
    timeout_s: float = REQUEST_TIMEOUT_S,
    expect_substring_display: str | None = None,
) -> ProbePhases:
    """
    Attempt *url* up to RETRY_ATTEMPTS times with exponential backoff.

    Why a hand-rolled loop instead of tenacity?
    -------------------------------------------
    tenacity's ``@retry`` decorator wraps a synchronous or async function with
    ``asyncio.sleep`` correctly only from tenacity ≥ 8.2.  However, to keep
    the dependency surface minimal and avoid any sync/async confusion, a manual
    loop is clearer, more explicit, and trivially understood by any on-call
    engineer reading this during an incident.

    Backoff schedule (RETRY_BACKOFF_BASE=2, cap=10):
      Attempt 1 fails → sleep  2.0 s
      Attempt 2 fails → sleep  4.0 s
      Attempt 3 fails → raise _ProbeFailure (no more sleep)

    Args:
        session:          Shared aiohttp.ClientSession.
        url:              Target URL.
        sample:           Out-of-band connection sample forwarded to _probe_once.
        expect_substring: Optional content assertion forwarded to _probe_once.
        expected_status:  Per-target expected HTTP status, forwarded to _probe_once.
        timeout_s:        Per-target per-attempt timeout, forwarded to _probe_once.
        expect_substring_display: Forwarded to _probe_once — see its docstring.

    Returns:
        The ProbePhases of the first successful attempt.

    Raises:
        _ProbeFailure: After all RETRY_ATTEMPTS are exhausted. exc.detail is
                       already redacted (see expect_substring_display), so
                       every log line below is safe to emit as-is.
    """
    last_exc: _ProbeFailure | None = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await _probe_once(
                session, url, sample, expect_substring, expected_status, timeout_s,
                expect_substring_display=expect_substring_display,
            )  # success
        except _ProbeFailure as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                sleep_s = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                logger.warning(
                    "Attempt %d/%d failed for %s (%s) — retrying in %.1fs",
                    attempt, RETRY_ATTEMPTS, url, exc.detail, sleep_s,
                )
                await asyncio.sleep(sleep_s)
            else:
                logger.error(
                    "Attempt %d/%d failed for %s (%s) — marking DOWN.",
                    attempt, RETRY_ATTEMPTS, url, exc.detail,
                )

    raise last_exc  # type: ignore[misc]  — always set after ≥1 iteration


# ---------------------------------------------------------------------------
# Diagnostics logging
# ---------------------------------------------------------------------------

def _fmt_ms(value: float | None) -> str:
    """Render an optional millisecond figure, or '—' when not measured."""
    return "—" if value is None else f"{value:.0f}ms"


def _log_diagnostics(url: str, phases: ProbePhases, findings: list) -> None:
    """
    Emit the per-phase breakdown and every diagnosis to the structured log.

    One phase line gives the on-call engineer the whole timing picture at a
    glance; one WARN/CRITICAL line per finding gives them the specific verdict
    and the fix. This is the 1 AM contract: the log alone must be enough to
    act on, without opening the source or re-running anything.
    """
    logger.info(
        "  ⏱  phases %s | dns=%s connect=%s rtt=%s tls=%s ttfb=%s "
        "server=%s transfer=%s body=%dB reused=%s cert_days=%s alpn=%s",
        url,
        _fmt_ms(phases.dns_ms),
        _fmt_ms(phases.connect_total_ms),
        _fmt_ms(phases.rtt_ms),
        _fmt_ms(phases.tls_ms),
        _fmt_ms(phases.ttfb_ms),
        _fmt_ms(phases.server_processing_ms),
        _fmt_ms(phases.transfer_ms),
        phases.body_bytes,
        phases.connection_reused,
        "—" if phases.tls_cert_days_left is None else phases.tls_cert_days_left,
        phases.alpn_protocol or "—",
    )

    for finding in findings:
        line = "  🔎  %s [%s] %s → %s"
        args = (url, finding.code, finding.evidence, finding.recommendation)
        if finding.severity == "critical":
            logger.error(line, *args)
        elif finding.severity == "warn":
            logger.warning(line, *args)
        else:
            logger.info(line, *args)


# ---------------------------------------------------------------------------
# Per-URL orchestration coroutine
# ---------------------------------------------------------------------------

async def check_url(
    url                     : str,
    session                 : aiohttp.ClientSession,
    state                   : StateManager,
    expect_substring        : str   | None = None,
    expected_status         : int   | None = None,
    timeout_s               : float | None = None,
    degraded_ttfb_ms        : float | None = None,
    degraded_rtt_ms         : float | None = None,
    expect_substring_display: str   | None = None,
) -> CheckOutcome:
    """
    Run the full health-check pipeline for one URL.

    Pipeline
    --------
    1. sample_connection()    →  out-of-band RTT + TLS + ALPN/h2 sample.
    2. _probe_with_backoff()  →  fires up to RETRY_ATTEMPTS async GETs, each
       one asserting expect_substring in the body when configured.
    3. analyze()              →  turn phase timings into findings.
    4. set_up / set_degraded / set_down  →  persist, detect transition.
    5. If transition detected →  send the matching Discord alert.

    A 200 does not always mean UP. If the probe succeeds but a performance
    finding fired (or TTFB crossed the degraded threshold), the target is
    DEGRADED — up and hurting — and gets an amber alert, distinct from the
    red DOWN alert, so triage is carried by the colour. And a 200 whose body
    fails the content assertion is not a success at all — it counts as a
    probe failure, same as a timeout or a 503.

    This coroutine is designed to be gathered concurrently with all others;
    it never touches shared mutable state outside the StateManager lock.

    Args:
        url:              Target URL to probe.
        session:          Shared aiohttp.ClientSession for HTTP I/O.
        state:            Shared StateManager for transition detection and persistence.
        expect_substring: Optional content assertion (see Target.expect_substring).
        expected_status:  Per-target expected HTTP status; None falls back to
                          the module default EXPECTED_STATUS.
        timeout_s:        Per-target per-attempt timeout; None falls back to
                          the module default REQUEST_TIMEOUT_S.
        degraded_ttfb_ms: Per-target DEGRADED threshold on TTFB; None falls
                          back to diagnostics.DEGRADED_TTFB_MS.
        degraded_rtt_ms:  Per-target DEGRADED/HIGH_RTT threshold on RTT; None
                          falls back to diagnostics.RTT_HIGH_MS.
        expect_substring_display: Forwarded to _probe_with_backoff — see
                          Target's docstring for the redaction contract.

    Returns:
        A CheckOutcome carrying the status actually persisted for this URL
        after this run — STATUS_UP, STATUS_DEGRADED, or STATUS_DOWN — plus
        the phases/findings (when available) for history persistence. A
        target still inside its DOWN_CONFIRMATIONS hysteresis window reports
        its preserved healthy status (UP/DEGRADED), not DOWN — this mirrors
        exactly what state.json itself says, so a --strict exit code never
        disagrees with the file an operator would read.
    """
    logger.info("─── Checking: %s", url)

    effective_expected_status = expected_status if expected_status is not None else EXPECTED_STATUS
    effective_timeout_s = timeout_s if timeout_s is not None else REQUEST_TIMEOUT_S
    effective_degraded_ttfb_ms = degraded_ttfb_ms if degraded_ttfb_ms is not None else DEGRADED_TTFB_MS
    effective_rtt_high_ms = degraded_rtt_ms if degraded_rtt_ms is not None else RTT_HIGH_MS

    # Sample the path out-of-band before the probe: a raw TCP connect for RTT
    # and (for HTTPS) a controlled TLS handshake for TLS time and ALPN/h2.
    # aiohttp's own trace hooks fuse TCP connect and TLS and only ever offer
    # http/1.1, so they can give neither a clean RTT/TLS split nor server h2
    # support. A None sample (firewall, refusal) only degrades diagnostics —
    # never the probe.
    sample: ConnectionSample | None = None
    host_port = host_port_from_url(url)
    if host_port is not None:
        host, port = host_port
        sample = await sample_connection(host, port, use_tls=url.lower().startswith("https"))

    try:
        phases = await _probe_with_backoff(
            session, url, sample, expect_substring,
            effective_expected_status, effective_timeout_s,
            expect_substring_display=expect_substring_display,
        )
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        findings = analyze(phases, rtt_high_ms=effective_rtt_high_ms)
        _log_diagnostics(url, phases, findings)
        diagnostics = phases_to_dict(phases, findings)

        reason = degraded_reason(phases, findings, degraded_ttfb_ms=effective_degraded_ttfb_ms)
        if reason is not None:
            transitioned = await state.set_degraded(url, reason, diagnostics=diagnostics)
            if transitioned:
                logger.warning("🟡  STATE CHANGE → DEGRADED  %s | %s", url, reason)
                await dispatch_alert(
                    session, url, status_detail=reason, kind=AlertKind.DEGRADED
                )
            else:
                logger.info("🟡  Still DEGRADED (alert suppressed)  %s | %s", url, reason)
            return CheckOutcome(
                url=url, status=STATUS_DEGRADED, error=reason, checked_at=checked_at,
                phases=phases, findings=findings,
            )
        else:
            transitioned = await state.set_up(url, diagnostics=diagnostics)
            if transitioned:
                logger.info("🟢  STATE CHANGE → UP    %s", url)
                await dispatch_alert(
                    session, url, status_detail="Service is UP", kind=AlertKind.RECOVERY
                )
            else:
                logger.info("✅  OK (no change)       %s", url)
            return CheckOutcome(
                url=url, status=STATUS_UP, error=None, checked_at=checked_at,
                phases=phases, findings=findings,
            )

    except _ProbeFailure as exc:
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        transitioned = await state.set_down(url, error=exc.detail)
        if transitioned:
            logger.error("🔴  STATE CHANGE → DOWN  %s | %s", url, exc.detail)
            await dispatch_alert(
                session, url, status_detail=exc.detail, kind=AlertKind.FAILURE
            )
        else:
            # Covers two cases: already-confirmed DOWN (repeat, alert already
            # sent), or a healthy target still inside its DOWN_CONFIRMATIONS
            # hysteresis window (not yet alerted). Either way: no alert here.
            logger.warning(
                "⚠️   Failure recorded, alert suppressed (already DOWN or "
                "pending confirmation)  %s | %s", url, exc.detail,
            )
        # The persisted status is authoritative: a pending-hysteresis failure
        # leaves the target's previous healthy status in place (see set_down),
        # so this can legitimately come back UP/DEGRADED, not just DOWN — with
        # exc.detail still carried as `error`, so history shows a probe that
        # failed this run even though the persisted status didn't flip.
        return CheckOutcome(
            url=url, status=state.current_status(url) or STATUS_DOWN, error=exc.detail,
            checked_at=checked_at,
        )


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    """
    Register SIGINT and SIGTERM handlers on *loop*.

    When a signal arrives:
      1. A one-line notice is logged.
      2. The shared ``shutdown_event`` is set.
      3. The main coroutine unblocks, awaits all in-flight tasks, then exits.

    Using loop.add_signal_handler() (POSIX only) integrates cleanly with the
    event loop — no threading.Event, no call_soon_threadsafe hacks needed.

    Note: Windows does not support add_signal_handler; the KeyboardInterrupt
    exception path acts as a fallback there.
    """
    def _handler(sig: signal.Signals) -> None:
        logger.info("Signal %s received — initiating graceful shutdown.", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler, sig)
        except (NotImplementedError, OSError):
            # Windows: add_signal_handler is not supported — fall through to
            # KeyboardInterrupt handling in __main__.
            logger.debug("Could not register signal %s (platform limitation).", sig.name)


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------

def _warn_if_over_budget(duration_s: float) -> None:
    """
    Log a WARNING when a completed run's wall-clock duration exceeded
    RUN_BUDGET_S — the signal that this scheduler interval (the compose loop's
    ``sleep 60``, a cron entry, a Kubernetes CronJob schedule) is causing
    overlapping runs, before an operator notices it any other way.
    """
    if duration_s > RUN_BUDGET_S:
        logger.warning(
            "Run took %.1fs, exceeding the %.0fs budget — if the scheduler "
            "(cron/compose loop) fires every %.0fs, runs are now overlapping.",
            duration_s, RUN_BUDGET_S, RUN_BUDGET_S,
        )


async def run_health_checks(
    targets     : list[str | Target] | None = None,
    state_path  : Path = STATE_FILE,
    history_path: Path = HISTORY_DB_FILE,
) -> int:
    """
    Orchestrate concurrent health checks across all configured targets.

    Concurrency model
    -----------------
    A single aiohttp.ClientSession is created for the entire run (reuses the
    underlying TCP connector / DNS cache).  One coroutine per URL is scheduled
    via asyncio.gather(return_exceptions=True), so all N checks run in parallel
    rather than sequentially.  ``return_exceptions=True`` ensures that an
    unhandled exception in one coroutine does not cancel the others.

    Graceful shutdown
    -----------------
    SIGINT / SIGTERM set a ``shutdown_event``.  Because asyncio.gather is
    awaited, all in-flight coroutines complete naturally before the session is
    closed and the loop exits.

    Args:
        targets:    Optional override; defaults to loading targets.yaml. Plain
                    URL strings are normalised to Target(url=..., expect_substring=None)
                    — this keeps direct calls and existing tests working without
                    an expect_substring, exactly as load_targets would for a
                    targets.yaml entry with no object form.
        state_path:   Path StateManager persists to. Defaults to the module's
                      STATE_FILE; tests and scripts that need an isolated state
                      file pass this explicitly rather than monkeypatching
                      StateManager's constructor default.
        history_path: Path HistoryRecorder persists to. Same isolation
                      rationale as state_path.

    Returns:
        The number of targets that are DOWN (or crashed their check) at the
        end of this run — 0 means every target is UP or DEGRADED. This is
        what --strict inspects to decide the process exit code; the script
        itself always exits 0 without --strict, since "some targets are
        down" is this monitor's normal operating condition, not a bug in it.
    """
    raw_targets: list[str | Target] = targets or load_targets()
    resolved: list[Target] = [
        t if isinstance(t, Target) else Target(url=t) for t in raw_targets
    ]
    state   = StateManager(state_path)
    history = HistoryRecorder(history_path)

    loop           = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    _install_signal_handlers(loop, shutdown_event)

    run_start      = time.monotonic()
    run_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info("=" * 64)
    logger.info(
        "SRE Health Checker v3 | targets=%d | concurrency=ALL | attempts=%d | backoff=%.0f–%.0fs",
        len(resolved), RETRY_ATTEMPTS, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX,
    )
    logger.info("=" * 64)

    # One shared session for the whole run — efficient TCP reuse.
    # The diagnostics TraceConfig records DNS and connection-create timings
    # into each request's trace_request_ctx dict; without it, dns_ms and
    # connect_total_ms come back None and the phase breakdown is partial.
    connector = aiohttp.TCPConnector(limit=100)
    trace_config = build_trace_config()
    up_count       = 0
    degraded_count = 0
    down_count     = 0
    async with aiohttp.ClientSession(
        connector=connector, trace_configs=[trace_config]
    ) as session:
        # Build one coroutine per target, then gather concurrently.
        tasks = [
            check_url(
                t.url, session, state,
                expect_substring=t.expect_substring,
                expected_status=t.expected_status,
                timeout_s=t.timeout_s,
                degraded_ttfb_ms=t.degraded_ttfb_ms,
                degraded_rtt_ms=t.degraded_rtt_ms,
                expect_substring_display=t.expect_substring_display,
            )
            for t in resolved
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        outcomes: list[CheckOutcome] = []
        for target, result in zip(resolved, results):
            if isinstance(result, Exception):
                # An unhandled exception is a bug in check_url, not a probe
                # failure it already handled — but it still means this target
                # was NOT confirmed healthy this run, so it counts toward
                # down_count too: a crash must never look like a clean pass
                # to a --strict caller.
                logger.critical(
                    "Unhandled exception for %s: %r — this is a bug, please report it.",
                    target.url, result,
                )
                down_count += 1
            else:
                outcomes.append(result)
                if result.status == STATUS_UP:
                    up_count += 1
                elif result.status == STATUS_DEGRADED:
                    degraded_count += 1
                else:
                    down_count += 1

    # History recording happens AFTER the session block closes — every alert
    # for this run has already been sent by this point. A history write
    # failure (full disk, locked file) must never be able to affect alerting,
    # and it can't: HistoryRecorder never raises (see history.py).
    await history.record_run(run_started_at, outcomes)
    await history.prune()

    if shutdown_event.is_set():
        logger.info("Shutdown signal was processed cleanly.")

    duration_s = time.monotonic() - run_start
    _warn_if_over_budget(duration_s)

    logger.info("=" * 64)
    logger.info(
        "Run complete: %d up, %d degraded, %d down | duration=%.1fs | State file: %s",
        up_count, degraded_count, down_count, duration_s, state_path.resolve(),
    )
    logger.info("=" * 64)

    return down_count


REPORT_WINDOWS_DAYS: tuple[int, ...] = (1, 7, 30)


def _generate_report(
    targets       : list[Target],
    history_path  : Path = HISTORY_DB_FILE,
    retention_days: int | None = None,
    now           : datetime | None = None,
) -> str:
    """
    Render the --report table: uptime% per target over every window in
    REPORT_WINDOWS_DAYS that fits inside the configured retention, plus TTFB
    p50/p95 over the widest window shown.

    A window longer than the actual retention is never displayed. Retention
    already deleted anything older than HISTORY_RETENTION_DAYS, so a "30d
    uptime" column when retention is 7 would either show a number quietly
    computed from 7 days of data under a 30-day label, or (worse) look
    identical to the 7d column and read as a bug. Neither is honest, so the
    column is omitted instead — retention is stated once in the header, not
    re-litigated per cell.

    Args:
        targets:        Targets to report on (normally load_targets()).
        history_path:   Path to history.db.
        retention_days: Override for tests; defaults to the same env-driven
                         value HistoryRecorder itself would use.
        now:            Override for tests; defaults to the real current time.
    """
    if retention_days is None:
        retention_days = _retention_days_from_env()
    if now is None:
        now = datetime.now(timezone.utc)

    windows = [w for w in REPORT_WINDOWS_DAYS if w <= retention_days] or [retention_days]
    widest  = max(windows)

    def since(days: int) -> str:
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    col_width = 12
    header = (
        "Target".ljust(40)
        + "".join(f"Up {w}d".rjust(col_width) for w in windows)
        + "p50 TTFB".rjust(col_width) + "p95 TTFB".rjust(col_width)
    )
    lines = [
        f"vigil-sre history report — retention: {retention_days}d "
        f"(windows beyond retention are not shown)",
        "",
        header,
        "-" * len(header),
    ]

    for target in targets:
        row = target.url.ljust(40)
        for w in windows:
            pct = uptime_pct(history_path, target.url, since(w))
            cell = "no data" if pct is None else f"{pct:.1f}%"
            row += cell.rjust(col_width)

        percentiles = latency_percentiles(history_path, target.url, since(widest))
        if percentiles is None:
            row += "no data".rjust(col_width) + "no data".rjust(col_width)
        else:
            row += (
                f"{percentiles['p50_ttfb_ms']:.0f}ms".rjust(col_width)
                + f"{percentiles['p95_ttfb_ms']:.0f}ms".rjust(col_width)
            )
        lines.append(row)

    return "\n".join(lines)


def _exit_code(strict: bool, down_count: int) -> int:
    """
    Compute the process exit code for a completed run.

    Without --strict this is always 0: "some targets are down" is this
    monitor's normal operating condition, not a bug in the monitor itself —
    a bare `python main.py` must not fail a cron job just because a target
    it watches is having a bad day. With --strict, a non-zero down_count
    becomes exit code 1 so a CI job or Kubernetes CronJob can act on it.
    """
    return 1 if (strict and down_count > 0) else 0


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--report" in sys.argv:
        # Mutually exclusive with a normal run: report and exit, probe
        # nothing. Reads history.db only — no session, no targets probed.
        print(_generate_report(load_targets()))
        sys.exit(0)

    strict = "--strict" in sys.argv
    # STATE_FILE_PATH lets a container deployment point state.json at a
    # bind-mounted DIRECTORY (see docker-compose.yml) instead of the bare
    # relative filename. A single-file bind mount cannot work here: state.json
    # is a mount point in that case, and the OS refuses to rename() a file
    # onto an active mount point (confirmed: EBUSY, reproducible on any OS,
    # not a Docker Desktop quirk) — which is exactly what StateManager's
    # atomic tmp-then-replace write does on every save. Unset, this resolves
    # to the same STATE_FILE default as always; local/non-Docker runs are
    # unaffected.
    state_path = Path(os.getenv("STATE_FILE_PATH", str(STATE_FILE)))
    try:
        down_count = asyncio.run(run_health_checks(state_path=state_path))
    except KeyboardInterrupt:
        # Windows fallback — SIGINT arrives as KeyboardInterrupt, not via
        # add_signal_handler.  Log and exit cleanly without a traceback.
        logger.info("KeyboardInterrupt received — exiting.")
        sys.exit(0)

    exit_code = _exit_code(strict, down_count)
    if exit_code != 0:
        logger.error(
            "--strict: exiting %d because %d target(s) are DOWN.",
            exit_code, down_count,
        )
    sys.exit(exit_code)
