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
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple, TypedDict

import aiohttp
import yaml

import targetstore
from dotenv import load_dotenv

from diagnostics import (
    TREND_DEGRADING,
    TREND_INCOMPARABLE,
    in_maintenance,
    TREND_IMPROVING,
    TREND_MIN_CHANGE_MS,
    TREND_MIN_CHANGE_PCT,
    TREND_MIN_SAMPLES,
    TREND_NO_DATA,
    TREND_TOO_FEW_SAMPLES,
    latency_trend,
    TREND_DEGRADING,
    reminders_due,
    CERT_CRIT_DAYS,
    DEGRADED_TTFB_MS,
    RTT_HIGH_MS,
    TTFB_BACKEND_SLACK_MS,
    ConnectionSample,
    ProbePhases,
    analyze,
    build_trace_config,
    cert_alert_threshold,
    cert_days_left,
    degraded_reason,
    host_port_from_url,
    is_cert_healthy,
    is_cert_renewed,
    phases_to_dict,
    sample_connection,
)
from history import (
    latency_trend_windows,
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
    reset_circuits,
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
#: Most bytes of response body one probe attempt will take, counted AFTER
#: decompression. The 5 s timeout bounds time, not size: on a fast path 5 s is
#: hundreds of megabytes per attempt per target per minute, which is egress
#: spent and, with the body held, a container killed by the OOM killer. A health
#: probe that downloads a file is not probing health.
#:
#: 4 MiB is 16x BANDWIDTH_MIN_SAMPLE_BYTES (256 KiB), so every body large enough
#: for the window-limited rule to fire is still read in full and still measured,
#: and well above any status endpoint or HTML landing page; below it the cap
#: never changes a verdict. It is a constant and not a setting because a value
#: nobody tuned is one nobody can misconfigure into disabling the guard.
MAX_BODY_BYTES    : int  = 4 * 1024 * 1024
BODY_CHUNK_BYTES  : int  = 64 * 1024       # read size; also bounds what is held at once
# One definition, owned by targetstore: api.py reads the same file to show the
# editor what this process is running on, and must not name it separately.
TARGETS_FILE      : Path = targetstore.SEED_FILE
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
# Notification confirmation is independent of the raw observed state.
DEGRADED_CONFIRMATIONS: int = 3
PERFORMANCE_RECOVERY_CONFIRMATIONS: int = 2
PERFORMANCE_MAX_GAP_S: int = 300


# Flap hysteresis: consecutive failed RUNS required to knock a known-healthy
# (UP or DEGRADED) target into confirmed DOWN. Retries inside _probe_with_backoff
# already absorb blips within one run; this absorbs flapping ACROSS runs, where
# a target bouncing UP/DOWN every run would otherwise fire an alert every run.
# A target with no prior history (first-ever observation) skips the hysteresis
# and fails straight to DOWN — there is no flapping to protect against on a
# first sample, and smoke-test targets that are deliberately always-down must
# still alert on their very first run.
DOWN_CONFIRMATIONS: int = 2

# v4 added cert_last_seen_days. v3 added cert_alerted_days.
# Both bumps are
# documentary, not functional: _load_sync only checks whether schema_version
# EXISTS, never its value, and UrlState is total=False — so a v2 file loads
# under v3 with the field simply absent, which is exactly the correct default
# ("never cert-alerted"). There is no migration step to run or forget.
STATE_SCHEMA_VERSION: int = 5   # bump whenever state.json's on-disk shape changes

# Webhook delivery retry now lives in notifiers.py alongside the Notifier
# base class that applies it — see WEBHOOK_RETRY_ATTEMPTS / _BASE_S /
# _AFTER_CAP_S there. It moved with the code it governs.

# If a full run takes longer than this, the docker-compose `sleep 60` loop
# (or an equivalent cron/systemd-timer interval) is scheduling overlapping
# runs without anyone knowing — two StateManager instances writing the same
# state.json concurrently is exactly the kind of silent corruption the
# asyncio.Lock protects against WITHIN one run, but not across two runs that
# were never supposed to overlap in the first place.
RUN_BUDGET_S: float = 60.0

# Dead-man's switch ping timeout. Deliberately short and never retried: the
# next run is 60s away and watchdog grace periods are minutes, so a missed
# ping self-heals. Spending run budget on it would defeat the purpose.
HEARTBEAT_TIMEOUT_S: float = 5.0


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
    #: Per-target backend threshold. The global 500 ms fired on 63.8% of the
    #: probes to cloudflare.com and on 0% of those to google.com, measured over
    #: 920 samples: a threshold below a target's median (587 ms) does not flag
    #: an anomaly, it flags the target's normal operation.
    degraded_backend_ms     : float | None = None
    #: Opt-in: re-alert on a schedule while this target stays DOWN. Off by
    #: default, and deliberately so — a target you added to watch something
    #: break (a test URL, a known-bad endpoint) would otherwise nag forever.
    remind                  : bool  = False
    #: Whether this target's p95 trend may raise an alert. On by default; set
    #: false for a target whose trend is not interpretable.
    #:
    #: httpbin.org is the case that forced this field. Measured over the 230
    #: collected probes per target, sliding 3-day windows: the natural p95 move
    #: between adjacent windows has a MEDIAN of 57.7% for httpbin against 5.7%
    #: for google.com and 12.4% for cloudflare.com. Its median sits above the
    #: 50% threshold, so at the global setting it would fire on 63.3% of
    #: evaluations -- the pathology the backend threshold had before its
    #: per-target calibration: a threshold below a target's median measures
    #: normal operation instead of an anomaly.
    #:
    #: Raising ITS threshold instead was the alternative, and it is the wrong
    #: fix. Silencing it needs ~165%, and httpbin is DOWN 38.7% of the time, so
    #: its p95 is computed over whichever probes happened to succeed. A +165%
    #: move on that sample does not mean "the backend got slower", it means
    #: "this window drew a different subset". The number would be calibrated
    #: and meaningless, and set so high it would not fire on real degradation
    #: either. The problem is sample validity, not magnitude, and no threshold
    #: fixes sample validity.
    trend_alerts            : bool  = True
    maintenance             : list[dict] | None = None


# Imported, not redeclared. The dashboard write path validates the same syntax
# before persisting, and it must be the SAME syntax: a pattern that drifts here
# would let the API accept what this function then refuses with sys.exit(1).
_ENV_VAR_REF     = targetstore.ENV_VAR_REF
_ENV_VAR_SUSPECT = targetstore.ENV_VAR_SUSPECT


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


def _seed_problem(path: Path) -> str | None:
    """
    Why *path* cannot be used as a seed, or None when it parses.

    Only for the branch where a valid store makes the YAML irrelevant, so it
    reports and never exits. The YAML branch keeps its own exits: with no store
    the file is the only source, and a round with no source must fail loudly.
    """
    if not path.exists():
        return "the file does not exist"
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return f"it is not valid YAML ({' '.join(str(exc).split())})"
    except (OSError, UnicodeDecodeError) as exc:
        return f"it cannot be read ({exc})"
    return None


def load_targets(
    # Resolved at call time, not bound at import. `path: Path = TARGETS_FILE`
    # evaluates the default once, when the module loads, so a later change to
    # TARGETS_FILE never reached it. Not live -- every call site passes the path
    # explicitly -- but it is the same shape that already caused a real bug in
    # targetstore, and it was cheap to close while still theoretical.
    path      : Path | None = None,
    store_path: Path | None = None,
) -> list[Target]:
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

    The dashboard-managed store is consulted FIRST. Once it exists the YAML is
    a seed nobody reads, so a missing or malformed one is a WARNING and not an
    exit; with no store the YAML is the only source and every failure above
    still exits 1. A store that exists but cannot be used also exits 1: the
    seed is never a substitute for it.

    Args:
        path: Path to the YAML targets file.
        store_path: Path to the dashboard-managed store; None for the default.

    Returns:
        Non-empty list of Target records.
    """
    path = path if path is not None else TARGETS_FILE

    # The dashboard-managed store wins when it exists, and it is read BEFORE the
    # YAML is opened. Once it exists the YAML is a seed nobody reads; checking
    # it first let a missing or malformed seed sys.exit(1) a round that had a
    # perfectly good store -- every target unprobed for a file that was not
    # being used. `None` (never written) and `[]` (every target deliberately
    # deleted) are different answers, and only `None` falls back — collapsing
    # them would resurrect a list the operator removed on the very next run.
    # store_path is a parameter and not just the module default so a test can
    # point it somewhere harmless. Without it this function reads whatever
    # data/targets.json happens to exist beside the process, which makes the
    # result depend on the cwd -- the same ambient-state trap the state file
    # path was given a parameter to escape.
    #
    # A store that EXISTS and cannot be used is neither of those: it fails the
    # round. Treating it as "never written" probed a stale seed -- targets added
    # in the dashboard stopped being probed, deleted ones came back, the round
    # exited 0 with a green heartbeat -- so there is no fallback to choose.
    try:
        stored = targetstore.read_store(store_path)
    except targetstore.StoreError as exc:
        logger.critical(
            "The dashboard-managed target list is unusable: %s. Not falling "
            "back to '%s': it is not the list the dashboard last saved, so "
            "probing it would drop the targets added since and bring back the "
            "ones removed. Fix the file, or delete it to return to the seed.",
            exc, path,
        )
        sys.exit(1)
    if stored is not None:
        store_file = store_path if store_path is not None else targetstore.STORE_FILE
        seed_problem = _seed_problem(path)
        if seed_problem is not None:
            # Not fatal, and not silent either: the operator who still edits
            # the YAML believes those edits are live, and they are not.
            logger.warning(
                "Ignoring '%s': %s. The dashboard-managed store '%s' is in use "
                "and the YAML is not read, so this does not affect the run.",
                path, seed_problem, store_file,
            )
        logger.info(
            "Loaded %d target(s) from '%s' (managed from the dashboard; "
            "'%s' is the seed and is no longer read).",
            len(stored), store_file, path,
        )
        return [
            Target(
                url                     =e["url"],
                expect_substring        =_resolve_expect_substring(
                    e["expect_substring"], store_file, e["url"])[0]
                    if e.get("expect_substring") else None,
                expect_substring_display=_resolve_expect_substring(
                    e["expect_substring"], store_file, e["url"])[1]
                    if e.get("expect_substring") else None,
                expected_status         =e.get("expected_status"),
                timeout_s               =e.get("timeout_s"),
                degraded_ttfb_ms        =e.get("degraded_ttfb_ms"),
                degraded_rtt_ms         =e.get("degraded_rtt_ms"),
                degraded_backend_ms     =e.get("degraded_backend_ms"),
                remind                  =e.get("remind", False),
                trend_alerts            =e.get("trend_alerts", True),
                maintenance             =e.get("maintenance"),
            )
            for e in stored
        ]

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
            # A bare string is a URL with no other field, and it gets the same
            # URL check the object form gets below. Skipping it let a string
            # entry carry a port or host the probe cannot dial.
            try:
                targetstore.validate_entry({"url": entry})
            except targetstore.ValidationError as exc:
                logger.critical("'%s' target %r: %s", path, entry, exc)
                sys.exit(1)
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
            degraded_backend_ms = _validated_numeric_field(
                entry, "degraded_backend_ms", path, numeric_types=(int, float),
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
            # remind, trend_alerts and maintenance are validated by targetstore
            # rather than here, on purpose. This loader and the store had drifted
            # into two sets of rules for the same YAML, and these three fields
            # fell through the gap: the store branch of this function passed
            # them, this branch did not, and they landed on the dataclass
            # defaults with no error and no warning. A fresh install -- one that
            # follows the documented flow and has never opened the dashboard, so
            # data/targets.json does not exist yet -- therefore lost its
            # maintenance windows, its reminders and its trend opt-outs silently.
            # The operator read "silenced" in the file they edited and got paged
            # at 2 AM anyway.
            #
            # Only these three go through validate_entry. Running the whole entry
            # through it would also re-check the numerics against the store's
            # bounds, which are tighter than this loader's, and turn a warning
            # into an abort for configs that load fine today.
            try:
                checked = targetstore.validate_entry({
                    "url": str(entry["url"]),
                    **{k: entry[k]
                       for k in ("remind", "trend_alerts", "maintenance")
                       if k in entry},
                })
            except targetstore.ValidationError as exc:
                logger.critical("'%s' target %r: %s", path, entry["url"], exc)
                sys.exit(1)

            targets.append(Target(
                url=str(entry["url"]),
                expect_substring=expect,
                expect_substring_display=expect_display,
                expected_status=expected_status,
                timeout_s=timeout_s,
                degraded_ttfb_ms=degraded_ttfb_ms,
                degraded_rtt_ms=degraded_rtt_ms,
                degraded_backend_ms=degraded_backend_ms,
                remind=checked["remind"],
                trend_alerts=checked["trend_alerts"],
                maintenance=checked.get("maintenance"),
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

    # The state is keyed by URL, so two entries for one URL are one record
    # written twice: two failures in a single round count as two consecutive
    # ones and confirm DOWN without the two-round hysteresis, history gains a
    # row per entry, and entries with different settings overwrite each other.
    # The store refuses this in validate_all; the YAML has to as well. Exact
    # string identity, because that is the key the state uses.
    seen: set[str] = set()
    for target in targets:
        if target.url in seen:
            logger.critical(
                "'%s' lists %r more than once. Each URL is one target (the "
                "state and the history are keyed by it); remove the duplicate "
                "and merge its settings into the entry you keep.",
                path, target.url,
            )
            sys.exit(1)
        seen.add(target.url)

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
    cert_alerted_days   : int    # threshold (30 or 7) this target was last cert-alerted at
    cert_last_seen_days : int    # previous run's tls_cert_days_left — a RISE means renewal
    alert_pending       : bool   # a STATUS alert (FAILURE/RECOVERY/DEGRADED) is owed: written WITH the transition, cleared once a channel accepts it
    remind_step         : int    # reminders already sent for the CURRENT outage
    notification_status : str    # last status whose notification was delivered
    slow_runs           : int
    healthy_runs        : int
    muted_alert_owed    : bool   # FAILURE/DEGRADED was muted by a maintenance window; announce the current state once it closes
    missed_outage       : dict   # {"start","end"} of a DOWN whose FAILURE reached nobody; announced once, cleared on delivery


#: Fields that describe something ORTHOGONAL to the current status, and must
#: therefore survive a status change. Everything else in a UrlState record is
#: rebuilt from scratch by set_up/set_degraded/set_down — those three replace
#: the record wholesale rather than merging, so a field not named here is
#: silently dropped on the next successful run, i.e. within 60 seconds.
#:
#: cert_alerted_days is the first such field: whether we already warned about
#: an expiring certificate has nothing to do with whether the service is UP
#: right now, and losing it would re-fire the certificate alert every single
#: run for 30 days.
#:
#: consecutive_failures is deliberately NOT here — it is state ABOUT the
#: status and must reset when the target recovers.
#: alert_pending belongs here for the same reason: whether the last alert was
#: actually delivered says nothing about whether the service is UP right now,
#: and losing it on the next write is exactly what made a lost alert
#: unrecoverable — the record would come back clean and every later run would
#: read "already alerted".
_CARRY_FORWARD_FIELDS: tuple[str, ...] = (
    "cert_alerted_days", "cert_last_seen_days", "alert_pending",
    # The trend suppression marker. Status-orthogonal for the same reason the
    # certificate fields are: a target that goes UP -> DEGRADED -> UP has not
    # resolved a p95 drift, so a status change must not re-arm the alert.
    #
    # Omitting it did not fail a single unit test and broke the feature
    # completely in the container. The status writer runs on every probe,
    # BEFORE _maybe_alert_trend, so the marker was wiped and rewritten every
    # cycle -- which re-sent the same alert every cycle, the exact storm the
    # marker exists to prevent. Caught by running the image twice against the
    # same state and history, not by the suite.
    "trend_alerted_p95_ms", "notification_status", "slow_runs", "healthy_runs")


def _owed_an_alert(previous: UrlState | None) -> bool:
    """
    True when a status alert for this target was decided but never confirmed
    delivered.

    The three setters below decide "should we alert" from whether the status
    CHANGED. That is correct only if a change always produced a delivered
    alert. When every channel is down it does not: the status advances,
    dispatch fails, and from the next run on the record reads "no change,
    already alerted" forever. The outage and the silence about it have the
    same cause, so nothing else can catch it.

    Treating an undelivered alert as a reason to alert again turns that into
    a self-healing loop: the retry happens on the next cycle, 60 seconds
    later, and stops on its own once a channel comes back.

    The same flag covers a process that died before it knew the outcome: the
    setter writes it in the transition's own write (see StateManager._arm), so
    "decided but not yet confirmed" and "tried and failed" read the same way
    and take the same path. Delivery is therefore at-least-once: a kill after
    a channel accepted the alert and before the flag was cleared sends it
    again, and that duplicate is the price of never losing the first.
    """
    return bool(previous and previous.get("alert_pending"))


def _carry_forward(previous: UrlState | None, record: UrlState) -> UrlState:
    """
    Copy the status-orthogonal fields from *previous* into *record*.

    One place to declare what outlives a status change, instead of five
    replacement sites to remember (see _CARRY_FORWARD_FIELDS for why that
    distinction is load-bearing).
    """
    if previous is None:
        return record
    for field_name in _CARRY_FORWARD_FIELDS:
        if field_name in previous:
            record[field_name] = previous[field_name]  # type: ignore[literal-required]
    return record


class ReminderClaim(NamedTuple):
    """
    A reminder taken off the schedule but not yet known to have arrived.

    Carries the step the claim found, because "undo" has to mean "exactly what
    it was before", not "one less": after a long monitor outage several
    reminders are owed at once, one is sent, and the step jumps by more than 1.
    """

    elapsed_h    : float   # how long the outage has lasted, for the message
    previous_step: int     # remind_step before the claim
    claimed_step : int     # remind_step the claim wrote


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
        #: Advisory alerts (certificate, trend, reminder) that reached nobody
        #: THIS run, by AlertKind value. Deliberately not part of the persisted
        #: record: each of those kinds has its own retry marker, and a count of
        #: what failed in this run is all the exit code needs. It lives here
        #: because the funnel already holds the state and the run already owns
        #: one instance; see _alert_unless_muted for why it is not alert_pending.
        self.undelivered_advisory: dict[str, int] = {}
        #: State writes that failed this run. _write_sync cannot raise (see
        #: there), so this counter is the only way the end of the run learns
        #: that what it is about to call "complete" was not saved.
        self.write_failures: int = 0
        #: URLs whose alert_pending was set by a transition in THIS process and
        #: whose delivery outcome is not known yet. In memory only, on purpose:
        #: it exists to tell the funnel which flag a maintenance mute may take
        #: back (the one this very transition armed) from which it must leave
        #: alone (an older alert that really did reach nobody). A process that
        #: dies loses it, and the persisted flag then reads as an older alert --
        #: which is the safe reading, because it gets retried.
        self._armed_by_transition: set[str] = set()

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
        """
        Atomic write: tmp file → rename.  Called inside the lock.

        Never raises, and that is deliberate: this runs inside check_url for one
        target, and an exception here would cost the alerts of every target
        that has not been probed yet. But "does not raise" used to mean "nobody
        finds out" -- the failure was a log line, and the run went on to send
        the success heartbeat and exit 0 with no state.json on disk. Every later
        round then starts from "no state", every target looks new, and the
        transition alerts it would have deduplicated go out again, while the
        watchdog stays green. The failure is therefore counted in
        write_failures, which the end of the run reads before it vouches for
        itself.
        """
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
            self.write_failures += 1
            logger.error("State write failed for '%s': %s", self._path, exc)

    # ------------------------------------------------------------------ Helpers

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _arm(self, url: str, record: UrlState, previous: UrlState | None) -> None:
        """
        Put the obligation to announce this transition INTO *record*, before the
        write that persists the transition itself.

        The flag used to be written only after the dispatch returned, which left
        a window of up to ~75 s (webhook retries) in which state.json said
        "DOWN, nothing owed". A process killed inside it never sent the alert
        and no later run could tell: the status had already advanced, so every
        round read "no change". One write now carries both facts, and the flag
        is cleared only by record_alert_outcome once a channel accepted the
        alert.

        Must be called inside the lock, on the record about to be stored.
        """
        if not _owed_an_alert(previous):
            self._armed_by_transition.add(url)
        record["alert_pending"] = True

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

    def missed_outage(self, url: str) -> dict | None:
        """The {"start","end"} of an outage owed a catch-up notice, else None."""
        record = self._state.get(url) or {}
        return record.get("missed_outage") or None

    def status_alert(self, url: str, kind: AlertKind, detail: str) -> tuple[AlertKind, str]:
        """
        The status alert actually owed after a transition: the catch-up notice
        when an unannounced outage just ended, otherwise *kind* and *detail*.
        """
        missed = self.missed_outage(url)
        if not missed:
            return kind, detail
        status = (self._state.get(url) or {}).get("status", STATUS_UP)
        return AlertKind.UNANNOUNCED_OUTAGE, (
            f"DOWN from {missed['start']} to {missed['end']} UTC, and the alert "
            f"never reached any channel. Current status: {status}."
        )

    def recovery_kind(self, url: str) -> AlertKind:
        record = self._state.get(url) or {}
        return (AlertKind.PERFORMANCE_RECOVERY
                if record.get("notification_status") == STATUS_DEGRADED
                else AlertKind.RECOVERY)

    def _performance_decision(self, previous: UrlState | None, status: str,
                              now: str) -> tuple[bool, dict]:
        """Keep observations immediate; confirm notification changes across runs.

        A five-minute sampling gap breaks consecutive evidence, not an incident.
        Legacy records infer the previously announced status when no retry is owed.
        """
        old = previous or {}
        notified = old.get("notification_status", (
            old.get("status", STATUS_UP) if not _owed_an_alert(previous) and not old.get("muted_alert_owed") else STATUS_UP))
        if notified not in (STATUS_UP, STATUS_DEGRADED, STATUS_DOWN):
            notified = STATUS_UP
        continuous = False
        try:
            age = (datetime.fromisoformat(now.replace("Z", "+00:00")) -
                   datetime.fromisoformat(old["last_checked"].replace("Z", "+00:00"))).total_seconds()
            continuous = 0 <= age <= PERFORMANCE_MAX_GAP_S and not old.get("consecutive_failures")
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        def streak(key, same, ceiling):
            n = old.get(key, 0)
            return min(n + 1, ceiling) if continuous and same and type(n) is int and n >= 0 else 1
        slow = streak("slow_runs", old.get("status") == STATUS_DEGRADED, DEGRADED_CONFIRMATIONS) if status == STATUS_DEGRADED else 0
        healthy = streak("healthy_runs", old.get("status") == STATUS_UP, PERFORMANCE_RECOVERY_CONFIRMATIONS) if status == STATUS_UP else 0
        extra = {"notification_status": notified, "slow_runs": slow, "healthy_runs": healthy}
        # Leaving DOWN when the FAILURE never reached a channel: there is no
        # closure for an opening nobody saw, so without this the outage ended in
        # silence and no one could ever learn it happened. One catch-up notice
        # is owed instead -- start, end, and that its alert was lost -- and it
        # stays owed (same start and end) until a channel accepts it.
        missed = old.get("missed_outage")
        if old.get("status") == STATUS_DOWN and notified != STATUS_DOWN:
            missed = {"start": old.get("last_changed") or old.get("last_checked") or now,
                      "end": now}
        if missed and (old.get("status") == STATUS_DOWN or _owed_an_alert(previous)):
            extra["missed_outage"] = missed
            return True, extra
        if notified == STATUS_DOWN:
            return True, extra  # Availability restoration is never held behind performance confirmation.
        if status == STATUS_DEGRADED:
            due = (notified != STATUS_DEGRADED and slow >= DEGRADED_CONFIRMATIONS)
            due = due or (_owed_an_alert(previous) and old.get("status") == STATUS_DEGRADED)
        else:
            due = ((notified == STATUS_DEGRADED and healthy >= PERFORMANCE_RECOVERY_CONFIRMATIONS)
                   or notified == STATUS_DOWN
                   or (_owed_an_alert(previous) and old.get("status") == STATUS_UP))
        if not due:
            # A changed observation supersedes an undelivered warning/recovery.
            # No closure is generated for a warning that never reached a channel.
            extra["alert_pending"] = False
        return due, extra

    async def set_up(self, url: str, diagnostics: dict | None = None,
                     *, stabilize: bool = False) -> bool:
        """
        Mark *url* as UP. With stabilize=True, confirm performance recovery
        independently of the immediate observation; raw setters retain transition mode.

        Args:
            url:         The monitored URL.
            diagnostics: Optional latency/BDP breakdown from diagnostics.
                         phases_to_dict(); persisted verbatim under the
                         ``diagnostics`` key when provided. None leaves any
                         previously stored breakdown untouched is NOT the
                         behaviour — a None simply omits the key on this write,
                         reflecting "measured UP but no breakdown captured".

        Returns:
            True  — previous state was DOWN/DEGRADED (a real recovery), or an
                    earlier alert was never confirmed delivered → send alert.
                    The obligation is written with this very transition.
            False — was already UP, or never seen before     → suppress alert.

        A first sighting is recorded (status, timestamps) but is NOT a
        recovery: recovering means it was down and now is not, and a target
        with no previous record was never down. Reading "no record" as a
        change sent a "Service Recovered" for every healthy target on a fresh
        install. unknown -> DOWN and unknown -> DEGRADED are different: those
        are problem states and keep alerting (see set_down, set_degraded).
        """
        async with self._lock:
            now      = self._utc_now()
            previous = self._state.get(url)
            # A retry of an undelivered alert is not a change of status, so it
            # must not move last_changed: that is when the status changed.
            changed  = previous is None or previous["status"] != STATUS_UP
            alert    = previous is not None and (changed or _owed_an_alert(previous))
            policy = {}
            if stabilize:
                alert, policy = self._performance_decision(previous, STATUS_UP, now)

            record = UrlState(
                status      =STATUS_UP,
                last_checked=now,
                last_changed=now if changed else previous["last_changed"],
                last_error  ="",
            )
            if diagnostics is not None:
                record["diagnostics"] = diagnostics

            self._state[url] = _carry_forward(previous, record)
            self._state[url].update(policy)
            if alert:
                self._arm(url, self._state[url], previous)
            self._write_sync()
            return alert

    async def set_down(self, url: str, error: str, *, stabilize: bool = False) -> bool:
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
            old = previous or {}
            notified = old.get("notification_status", (
                old.get("status", STATUS_UP) if not _owed_an_alert(previous)
                and not old.get("muted_alert_owed") else STATUS_UP))

            if previous is None:
                self._state[url] = UrlState(
                    status               =STATUS_DOWN,
                    last_checked         =now,
                    last_changed         =now,
                    last_error           =error,
                    consecutive_failures =1,
                )
                self._arm(url, self._state[url], previous)
                if stabilize:
                    self._state[url].update(notification_status=notified, slow_runs=0, healthy_runs=0)
                self._write_sync()
                return True

            if previous["status"] == STATUS_DOWN:
                self._state[url] = _carry_forward(previous, UrlState(
                    status               =STATUS_DOWN,
                    last_checked         =now,
                    last_changed         =previous["last_changed"],
                    last_error           =error,
                    consecutive_failures =previous.get("consecutive_failures", 1) + 1,
                    # Carried by hand rather than via _CARRY_FORWARD_FIELDS,
                    # for the same reason consecutive_failures is: this counts
                    # reminders for THIS outage, so it must survive a repeat
                    # failure and vanish the moment the target recovers. A
                    # carried-forward field would follow the record into UP and
                    # silence the first reminder of the NEXT outage.
                    remind_step          =previous.get("remind_step", 0),
                ))
                # Same hand-carry as remind_step, for the same reason: it
                # describes THIS outage (a muted FAILURE not yet announced), so
                # it survives a repeat failure and is dropped by set_up.
                if previous.get("muted_alert_owed"):
                    self._state[url]["muted_alert_owed"] = True
                if stabilize:
                    self._state[url].update(notification_status=notified, slow_runs=0, healthy_runs=0)
                self._write_sync()
                # Normally False: a repeat failure on an already-DOWN target is
                # not news. It IS news when the alert that announced the outage
                # never landed.
                if _owed_an_alert(previous):
                    return True
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
                if stabilize:
                    self._state[url].update(notification_status=notified, slow_runs=0, healthy_runs=0)
                self._write_sync()
                return False

            self._state[url] = _carry_forward(previous, UrlState(
                status               =STATUS_DOWN,
                last_checked         =now,
                last_changed         =now,
                last_error           =error,
                consecutive_failures =strikes,
            ))
            self._arm(url, self._state[url], previous)
            if stabilize:
                self._state[url].update(notification_status=notified, slow_runs=0, healthy_runs=0)
            self._write_sync()
            return True

    async def prune(self, urls: list[str]) -> list[str]:
        """
        Drop state for targets no longer configured. Returns what was removed.

        Nothing else removes these records, and they are not inert. Two things
        read the whole map rather than the configured list:

          - ``stale_seconds`` answers with the age of the OLDEST record, so a
            single abandoned target pins the dashboard's freshness banner to
            "dead" forever — the one indicator this project calls its own, made
            permanently red and therefore ignorable.
          - The dashboard renders a row per record, so it shows endpoints
            nobody is watching, with an uptime that stopped moving.

        Reproduced live: two fixtures removed from the config kept a running
        monitor reading "these data are 978 minutes old" while it had probed
        sixty seconds earlier. The page contradicted itself in the same
        viewport — header "6 target(s)", editor listing 4.

        The dashboard editor turned removing a target into one click, so this
        went from a rare state to the expected one.

        Called at the end of a run, not the start: a target removed mid-run has
        already been probed, and dropping its record first would throw away the
        result of work that was done.
        """
        async with self._lock:
            configured = set(urls)
            removed    = [url for url in self._state if url not in configured]
            if not removed:
                return []
            for url in removed:
                del self._state[url]
            self._write_sync()
        logger.info(
            "Pruned %d target(s) no longer configured: %s. event_type=metric "
            "metric=state_pruned_total value=%d",
            len(removed), ", ".join(removed), len(removed),
        )
        return removed

    def note_undelivered_advisory(self, kind: AlertKind) -> None:
        """
        Count one advisory alert (anything that is not a state-change alert)
        that reached nobody this run. Synchronous and lock-free: it touches only
        the in-memory tally, never a URL record, and the event loop is single
        threaded.

        Keyed by the kind's value, which is also what the exit log prints, so a
        kind added later is counted and named without a list to extend.
        """
        name = str(getattr(kind, "value", kind))
        self.undelivered_advisory[name] = self.undelivered_advisory.get(name, 0) + 1

    async def claim_reminder_step(self, url: str) -> ReminderClaim | None:
        """
        Claim the reminder owed for *url*, or None if nothing is owed.

        Claims rather than asks: when one is owed the counter advances inside
        the same lock, so two concurrent probes of the same target cannot both
        decide to send it.

        The schedule is compared as a COUNT, not as "is one due right now".
        A monitor that was itself down for six hours comes back, sees three
        reminders were owed and sends one — not three, and not zero. Asking
        "is one due in this window" would skip a reminder forever whenever a
        run did not happen to land inside it.

        The claim is made BEFORE the reminder is sent, and that is the one
        place this method marks before it confirms. It has to: advancing the
        counter after the send would let two probes both read the old value.
        So the claim carries what it needs to be undone -- the step it found --
        and the caller hands it back through release_reminder when the reminder
        reached nobody. A claim that is never released after a failed send is a
        reminder deleted from the schedule.
        """
        async with self._lock:
            record = self._state.get(url)
            if record is None or record.get("status") != STATUS_DOWN:
                return None
            try:
                started = datetime.strptime(
                    record["last_changed"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                return None

            elapsed_h = (datetime.now(timezone.utc) - started).total_seconds() / 3600
            due       = reminders_due(elapsed_h)
            previous  = record.get("remind_step", 0)
            if due <= previous:
                return None

            record["remind_step"] = due
            self._write_sync()
            return ReminderClaim(
                elapsed_h=elapsed_h, previous_step=previous, claimed_step=due)

    async def claim_reminder(self, url: str) -> float | None:
        """
        The outage's age in hours if a reminder is owed, else None.

        The value-only view of claim_reminder_step, for a caller that will never
        hand the claim back. The production path does not use it: it has to be
        able to release.
        """
        claim = await self.claim_reminder_step(url)
        return None if claim is None else claim.elapsed_h

    async def release_reminder(self, url: str, claim: ReminderClaim) -> None:
        """
        Give back a claimed reminder that was not delivered.

        Restores the step the claim FOUND, so the next run offers the same
        reminder again as a STILL_DOWN. Before this existed the counter stayed
        advanced and the schedule never offered that step again; the one thing
        that "rescued" the lost reminder was the shared alert_pending flag
        re-sending it as a FAILURE -- the wrong message, announcing an outage
        that was hours old as if it had just begun.

        A no-op unless the record is still the outage the claim was made for:
        still DOWN, with remind_step still at the claimed value. A recovery
        replaces the record and deliberately does not carry remind_step, and
        writing the old counter back into it would silence the first reminder
        of the NEXT outage.
        """
        async with self._lock:
            record = self._state.get(url)
            if record is None or record.get("status") != STATUS_DOWN:
                return
            if record.get("remind_step", 0) != claim.claimed_step:
                return
            record["remind_step"] = claim.previous_step
            self._write_sync()

    async def record_alert_outcome(self, url: str, delivered: bool) -> None:
        """
        Persist whether the alert just dispatched for *url* reached anyone.

        Called after EVERY dispatch, success or failure, because both answers
        matter: a False arms the retry on the next run, and a True disarms one
        that was pending. Writing only on failure would leave the flag stuck
        and re-alert forever once a channel recovered.

        Never raises. The alert already happened (or already failed); losing
        the bookkeeping must not also cost the probe result that produced it.

        The flag a setter armed is cleared here and nowhere else (bar the
        maintenance mute, see release_muted_status_alert): only a confirmed
        delivery may say the obligation is met.
        """
        async with self._lock:
            self._armed_by_transition.discard(url)
            record = self._state.get(url)
            if record is None:
                # No record to annotate — the setter that should have created
                # one did not run. Nothing to do, and nothing worth failing a
                # run over.
                return
            if delivered:
                record["notification_status"] = record["status"]
                record.pop("missed_outage", None)  # type: ignore[misc]
            else:
                record.setdefault("notification_status", STATUS_UP)
            record["alert_pending"] = not delivered
            self._write_sync()


    async def release_muted_status_alert(self, url: str) -> None:
        """
        Take back the obligation a setter armed for an alert that a maintenance
        window then muted on purpose.

        A mute is not a failed delivery: queueing it would send every muted
        alert the moment the window closes (muted_alert_owed already announces
        the one that still matters). So the flag armed by THIS transition goes
        away. A flag that was already set before this transition is an older
        alert that really reached nobody, and the window has no business
        erasing the only record of it -- that case is not in
        _armed_by_transition and is left alone.
        """
        async with self._lock:
            if url not in self._armed_by_transition:
                return
            self._armed_by_transition.discard(url)
            record = self._state.get(url)
            if record is None or not record.get("alert_pending"):
                return
            record.pop("alert_pending", None)  # type: ignore[misc]
            self._write_sync()

    async def set_degraded(
        self, url: str, reason: str, diagnostics: dict | None = None,
        *, stabilize: bool = False
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
            # Same split as set_up: a retry alerts again without becoming a new
            # transition, so it leaves last_changed alone.
            changed  = previous is None or previous["status"] != STATUS_DEGRADED
            alert    = changed or _owed_an_alert(previous)
            policy = {}
            if stabilize:
                alert, policy = self._performance_decision(previous, STATUS_DEGRADED, now)

            record = UrlState(
                status      =STATUS_DEGRADED,
                last_checked=now,
                last_changed=now if changed else previous["last_changed"],
                last_error  =reason,
            )
            if diagnostics is not None:
                record["diagnostics"] = diagnostics
            # Only within the SAME degradation: from UP or DOWN this is a new
            # transition and check_url decides the marker from its outcome.
            if (previous is not None and previous["status"] == STATUS_DEGRADED
                    and previous.get("muted_alert_owed")):
                record["muted_alert_owed"] = True

            self._state[url] = _carry_forward(previous, record)
            self._state[url].update(policy)
            if alert:
                self._arm(url, self._state[url], previous)
            self._write_sync()
            return alert

    async def record_muted_alert(self, url: str, owed: bool) -> None:
        """
        Persist whether a FAILURE/DEGRADED for *url* was muted and still owes
        an announcement. Written only by check_url, from the outcome of the
        alert it just attempted:

          - MUTED (inside a window)  -> owed=True, announce after it closes;
          - DELIVERED or UNDELIVERED -> owed=False. Delivered: the current
            state was announced. Undelivered: alert_pending (written by the
            funnel) now owns the retry, and two markers for one lost alert
            would announce it twice.

        Cleared AFTER the dispatch, never before: a marker dropped ahead of a
        delivery that then fails is an outage nobody was ever told about.

        A no-op when the record is missing or already in that state.
        """
        async with self._lock:
            record = self._state.get(url)
            if record is None:
                return
            if bool(record.get("muted_alert_owed")) == owed:
                return
            if owed:
                record["muted_alert_owed"] = True
            else:
                record.pop("muted_alert_owed", None)  # type: ignore[misc]
            self._write_sync()

    def muted_alert_owed(self, url: str) -> bool:
        """True when a muted FAILURE/DEGRADED for this target is still unannounced."""
        record = self._state.get(url)
        return bool(record and record.get("muted_alert_owed"))


    async def record_cert_alert(
        self,
        url           : str,
        threshold_days: int | None,
        *,
        last_seen_days: int | None = None,
    ) -> None:
        """
        Persist the certificate threshold this target was last alerted at
        (None clears it), and the days_left observed this run.

        Separate from the status writers on purpose: certificate expiry is
        orthogonal to whether the service is up right now, so it does not
        belong in set_up/set_degraded/set_down's decision logic. Both fields
        survive those writers via _carry_forward.

        A no-op when the record does not exist yet — the status writer always
        runs first in check_url, so that only happens if a caller inverts the
        order, and inventing a bare record here would hide that mistake.
        """
        async with self._lock:
            record = self._state.get(url)
            if record is None:
                return
            if threshold_days is None:
                record.pop("cert_alerted_days", None)  # type: ignore[misc]
            else:
                record["cert_alerted_days"] = threshold_days
            if last_seen_days is not None:
                record["cert_last_seen_days"] = last_seen_days
            self._write_sync()

    def cert_alerted_days(self, url: str) -> int | None:
        """The threshold this target was last cert-alerted at, or None."""
        record = self._state.get(url)
        return record.get("cert_alerted_days") if record else None

    def cert_last_seen_days(self, url: str) -> int | None:
        """The tls_cert_days_left observed on this target's previous run."""
        record = self._state.get(url)
        return record.get("cert_last_seen_days") if record else None

    async def record_trend_alert(self, url: str, recent_p95_ms: float | None) -> None:
        """
        Persist the p95 this target was last trend-alerted at (None clears it).

        This is what makes the trend alert fire once per episode instead of
        once per run. A 7-day window compared hourly shares ~99% of its data
        with the comparison an hour earlier, so the verdict does not change --
        an alert keyed on the verdict alone would re-send the same sentence
        every hour until the drift aged out of the window. That is how a real
        signal gets muted by the person receiving it.

        Cleared on any non-degrading verdict, which is what arms the next
        episode. Separate from cert_alerted_days on purpose: sharing one marker
        would let a recovered trend unsuppress a certificate alert.
        """
        async with self._lock:
            record = self._state.get(url)
            if record is None:
                return
            if recent_p95_ms is None:
                record.pop("trend_alerted_p95_ms", None)  # type: ignore[misc]
            else:
                record["trend_alerted_p95_ms"] = recent_p95_ms
            self._write_sync()

    def trend_alerted_p95(self, url: str) -> float | None:
        """The p95 this target was last trend-alerted at, or None."""
        record = self._state.get(url)
        return record.get("trend_alerted_p95_ms") if record else None



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

            # The body is counted as it arrives and thrown away, never joined:
            # `await resp.read()` held all of it, decompressed, so one endpoint
            # that started returning a huge body (a misrouted download, a gzip
            # bomb) exhausted the container's memory and the OOM killer took
            # every other target with it. The cap is on the bytes delivered
            # AFTER decompression -- Content-Length is absent on chunked and
            # close-delimited responses and describes the compressed size on a
            # gzip one -- and exceeding it is a probe failure for this target,
            # never a partial read reported as UP. Leaving the `async with` on
            # the raise closes the connection with the rest unread.
            #
            # expect_substring is searched across chunk boundaries with only
            # the last len(needle)-1 bytes carried over, so a needle split by
            # the chunking is found without keeping the body to look for it.
            # The whole body is still read after a match, so body_bytes and the
            # transfer time stay the measurement of the full response.
            needle  = (expect_substring.encode("utf-8")
                       if expect_substring is not None else None)
            found   = not needle                    # None or empty: nothing to find
            overlap = len(needle) - 1 if needle else 0
            tail    = b""
            body_bytes = 0

            transfer_start = time.monotonic()
            async for chunk in resp.content.iter_chunked(BODY_CHUNK_BYTES):
                body_bytes += len(chunk)
                if body_bytes > MAX_BODY_BYTES:
                    raise _ProbeFailure(
                        f"Response body exceeded {MAX_BODY_BYTES} bytes "
                        f"(a health probe reads at most that much); aborted "
                        f"without reading the rest"
                    )
                if not found:
                    window = tail + chunk
                    found  = needle in window
                    tail   = window[-overlap:] if overlap else b""
            transfer_ms = (time.monotonic() - transfer_start) * 1000.0

            if not found:
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
    except UnicodeError as exc:
        # aiohttp wraps most bad URLs in ClientError but lets the IDNA codec's
        # UnicodeError out of its resolver ("https://example..com/"). Uncaught
        # it skipped the retry loop and surfaced as a crashed check, which is
        # an internal error for the whole round; a URL that cannot be resolved
        # is an answer, and the answer is DOWN.
        raise _ProbeFailure(f"UnicodeError: {exc}")

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

async def _maybe_alert_cert_expiry(
    session: aiohttp.ClientSession,
    url    : str,
    state  : StateManager,
    phases : ProbePhases,
    maintenance: list[dict] | None = None,
) -> None:
    """
    Fire a certificate-expiry alert if this run crossed a threshold, and stay
    silent on every run that did not.

    This is the one alert in the system that does NOT come from a state
    transition: a certificate 20 days from expiry leaves the service
    perfectly UP, so nothing in the UP/DEGRADED/DOWN machine would ever
    mention it. diagnostics.py has measured tls_cert_days_left from the start
    and deliberately excludes CERT_EXPIRING from the codes that degrade a
    target — which meant that until now the finding was logged, persisted,
    and read by nobody. This function is the missing channel.

    Never raises: dispatch_alert owns that guarantee, and the state write is
    a plain dict update under the existing lock.

    The threshold is recorded AFTER the alert reaches a channel, and only then.
    It used to be recorded before, to close a crash window — a crash mid-dispatch
    would otherwise re-alert next run. That reasoning held only while "dispatched"
    and "received" were treated as the same event. They are not: a maintenance
    window suppresses the send entirely, and recording the band anyway made the
    state claim a warning nobody got, permanently, because the band never comes
    round again. A freeze covering both the 30-day and the 7-day crossing could
    let the certificate expire with no delivered alert at all.

    days_left is written separately and always, because is_cert_renewed compares
    it across runs and a gap there would miss a renewal.
    """
    days = phases.tls_cert_days_left
    if days is None:
        return  # no TLS, so no certificate to have an opinion about

    last_seen = state.cert_last_seen_days(url)
    stored    = state.cert_alerted_days(url)

    # Two independent reset signals, because one is not enough:
    #   - is_cert_healthy: the certificate is comfortably far out again.
    #   - is_cert_renewed: days_left ROSE, which only happens on replacement.
    # The second exists because a cert renewed to anything under 30 days
    # never satisfies the first, and without it a target that already alerted
    # at the 7-day threshold stays silent forever — through the next full
    # decay and through the certificate actually expiring. Reproduced across
    # three simulated renewal cycles.
    if is_cert_renewed(days, last_seen) and stored is not None:
        logger.info(
            "🔐  Certificate renewed for %s (%d days left, was %d) — clearing "
            "alert suppression.", url, days, last_seen,
        )
        stored = None

    if is_cert_healthy(days):
        await state.record_cert_alert(url, None, last_seen_days=days)
        return

    threshold = cert_alert_threshold(days, stored)

    # days_left is persisted on its own, and always when it moved: is_cert_renewed
    # compares it against the next run's value, so a gap here would miss a
    # renewal. It changes about once a day, so the ~1,440 runs in between write
    # nothing at all. The alerted BAND is deliberately not written here -- see
    # below for why it waits.
    if days != last_seen:
        await state.record_cert_alert(url, stored, last_seen_days=days)

    if threshold is None:
        return  # already alerted in this band — silence is the feature

    critical = threshold == CERT_CRIT_DAYS
    detail   = f"tls_cert_days_left={days} (alerting below {threshold})"
    logger.warning(
        "🔐  CERTIFICATE %s  %s | %s",
        "CRITICAL" if critical else "EXPIRING", url, detail,
    )
    # Not wired to alert_pending, and the funnel enforces it: certificate alerts
    # are suppressed by cert_alerted_days, a separate mechanism with its own
    # reset condition (a RISE in days left). Sharing the flag lets a lost cert
    # alert re-fire a RECOVERY/DEGRADED/FAILURE the target had no new reason
    # for, and -- worse -- lets a DELIVERED cert alert erase the record that a
    # status alert never arrived. This comment used to say so while the call
    # below passed `state` and did the opposite, which is how both happened.
    outcome = await _alert_unless_muted(
        session, url, detail,
        AlertKind.CERT_EXPIRING_CRIT if critical else AlertKind.CERT_EXPIRING_WARN,
        maintenance, state,
    )
    # The band is recorded only once the warning reached a channel. Recording it
    # unconditionally is what made a maintenance window eat the 30-day notice:
    # the state said "already alerted in this band" and nobody had been. A deploy
    # freeze covering both the 30-day and the 7-day crossing could then let the
    # certificate expire without a single delivered alert, while state.json
    # showed the operator had been warned in time. Nothing logged it as an
    # error, and the symptom arrives weeks later as an outage.
    if outcome is AlertOutcome.DELIVERED:
        await state.record_cert_alert(url, threshold, last_seen_days=days)


async def _maybe_alert_trend(
    targets     : list[Target],
    state       : StateManager,
    history_path: Path,
    now         : datetime | None = None,
) -> int:
    """
    Alert on targets whose p95 is drifting worse, once per episode. Returns the
    number of alerts that reached a channel — not the number attempted, because
    an alert nobody received is one this function has to send again.

    Runs once per cycle rather than once per probe, and after history is
    recorded. Per-probe was the obvious place and it is wrong twice: a 7-day
    trend does not change between hourly probes, so it would be four database
    queries per cycle for a number that moves once a day, and check_url has no
    history_path in scope because nothing in it needed one.

    The whole feature was already built and wired to nothing. latency_trend has
    computed verdicts since trend detection shipped, and the only consumer was the --report
    section -- a signal nobody sees unless they run a command by hand. The
    threshold was never the missing piece; the channel was.

    Why the 50% threshold survives calibration unchanged: measured over the 230
    collected probes per target with sliding 3-day windows, the natural p95 move
    between adjacent windows reaches a p95 of 23.7% for google.com and 36.9% for
    cloudflare.com. 50% sits above the entire observed range of both, so a
    healthy target does not trip it. github.com crosses it on 6.2% of
    evaluations, all from one genuine spike -- firing there is the feature
    working, not a false positive.

    Never raises, and that now covers the history read as well as the dispatch.
    The earlier version said "never raises" while calling latency_trend_windows
    unguarded: latency_percentiles only swallows the missing-schema
    OperationalError and re-raises every other one, `database is locked`
    included. Under compose the dashboard and the probe share history.db, so a
    second writer -- another run, a backup, an antivirus -- could take the whole
    cycle down before state.prune() or the exit code, and `python main.py ||
    exit 1` would restart the container into a crash-loop. An advisory alert
    cannot be allowed to kill the probing it advises on. The same holds for a
    history file that is not a database at all (sqlite3.DatabaseError): any
    sqlite3.Error at the read skips the trend for the round.
    """
    candidates: list[tuple[Target, dict]] = []
    for target in targets:
        # Checked before the query, not after. A target that cannot produce an
        # interpretable trend should not cost two percentile scans per cycle to
        # find that out again.
        if not target.trend_alerts:
            continue
        # No state record means nowhere to persist the marker: record_trend_alert
        # is a deliberate no-op for an unknown url. Without this guard the
        # suppression read returns None for the same reason the write does
        # nothing, so the alert fires every cycle and never remembers -- up to
        # 1,440 messages a day for one p95. Happens when state.json is lost or
        # reset while history.db survives (separate volumes under compose), or
        # when check_url raised for this target before writing any status.
        if state.current_status(target.url) is None:
            continue
        try:
            recent, previous = latency_trend_windows(
                history_path, target.url, TREND_WINDOW_DAYS, now
            )
        except sqlite3.Error as exc:
            # Skipped for this round, not fatal. The trend is advisory; the
            # probes are not, and they have already run by the time this does.
            #
            # sqlite3.Error and not OperationalError: a file that is not a
            # database (a truncated copy, a partial restore) raises
            # DatabaseError, which is a sibling of OperationalError and not a
            # subclass, and it escaped this handler -- past the alerts, but
            # also past state.prune() and the heartbeat. Not `Exception`
            # either: a bug in the trend code has to stay loud.
            #
            # Once per round and out, not once per target: every target reads
            # the same file and fails the same way, so the first failure is the
            # whole answer. Nothing below ran for this target, so no verdict
            # exists and the suppression marker is left exactly as it was --
            # clearing it here would re-arm the episode and repeat the alert
            # the moment the file is readable again.
            logger.error(
                "Trend check skipped this round: the history database could "
                "not be read (%s: %s). No trend alert is sent and no trend "
                "marker is changed until it can be. "
                "event_type=metric metric=trend_checks_skipped_total value=1",
                type(exc).__name__, exc,
            )
            return 0
        verdict = latency_trend(
            recent["p95_ttfb_ms"]   if recent   else None,
            previous["p95_ttfb_ms"] if previous else None,
            recent["samples"]       if recent   else 0,
            previous["samples"]     if previous else 0,
            # The denominators. Without them latency_trend cannot tell a slower
            # backend from a window that simply lost fewer probes.
            recent["total"]         if recent   else 0,
            previous["total"]       if previous else 0,
        )
        if verdict["verdict"] != TREND_DEGRADING:
            # Any other verdict arms the next episode. This includes ok,
            # too_few_samples and no_data: a target that stopped producing
            # comparable windows has not "stayed degrading", it has stopped
            # being measurable, and holding the marker would swallow the alert
            # for the next real drift.
            if state.trend_alerted_p95(target.url) is not None:
                await state.record_trend_alert(target.url, None)
            continue
        # Silence within an episode is the feature, but "the same episode" is
        # not the same as "the same magnitude". A backend that drifts 400 -> 900
        # -> 2000 -> 5000 ms over a month never returns a non-degrading verdict,
        # so the marker never clears and the only alert ever sent is the first
        # one -- the smallest problem of the month.
        #
        # So one more alert is owed when the number gets materially worse than
        # what was already announced. The factor reuses the relative floor
        # rather than inventing a second dial: TREND_MIN_CHANGE_PCT is what this
        # project measured to sit above the natural variance of a healthy
        # target, so "another 50% on top of what we already said" is the same
        # bar applied to the announcement instead of to the window.
        alerted = state.trend_alerted_p95(target.url)
        if alerted is not None and verdict["recent_p95_ms"] < alerted * (
                1.0 + TREND_MIN_CHANGE_PCT / 100.0):
            continue
        candidates.append((target, verdict))

    if not candidates:
        return 0

    # One session for the alerts that actually fire. Opened here because the
    # run's session closed before history was recorded, and the trend cannot be
    # read until it is.
    delivered = 0
    async with aiohttp.ClientSession() as session:
        for target, verdict in candidates:
            detail = (
                f"p95 {verdict['previous_p95_ms']:.0f}ms → "
                f"{verdict['recent_p95_ms']:.0f}ms "
                f"({verdict['change_pct']:+.0f}%, {verdict['change_ms']:+.0f}ms) "
                f"over {TREND_WINDOW_DAYS}d vs the {TREND_WINDOW_DAYS}d before"
            )
            logger.warning("📉  TREND DEGRADING  %s | %s", target.url, detail)
            outcome = await _alert_unless_muted(
                session, target.url, detail,
                AlertKind.TREND_DEGRADING, target.maintenance, state,
            )
            # Recorded AFTER the alert reached a channel, and only then. This
            # reverses the order the certificate alert uses, deliberately.
            #
            # That order was chosen to close a crash window: writing afterwards
            # means a crash mid-dispatch re-alerts next run. True, and the wrong
            # trade. Writing BEFORE means a maintenance window -- or a rotated
            # webhook -- makes the marker claim an alert nobody received, and
            # the marker then suppresses that episode forever, including long
            # after the window closed. One trade costs a duplicate message; the
            # other loses the only warning a slow degradation ever gets.
            #
            # MUTED does not record either, so the episode stays armed and is
            # announced on the first cycle outside the window.
            if outcome is AlertOutcome.DELIVERED:
                await state.record_trend_alert(target.url, verdict["recent_p95_ms"])
                delivered += 1
    return delivered


class AlertOutcome(Enum):
    """
    What happened to one alert at the funnel.

    Replaces a bool that answered "is nothing owed". That bool was True both
    for a delivered alert and for a deliberately silenced one, which is the
    same answer to the retry question and the OPPOSITE answer to "may the
    caller now record this as communicated". The certificate and trend
    markers needed the second question and only had the first, so a
    maintenance window made them record an alert nobody received — and the
    marker then suppressed the alert forever, including after the window
    closed.

    Same reasoning that produced AlertKind: two states encoded in one boolean,
    resolved by a rule that lived in a docstring, until the case the rule did
    not cover showed up.
    """

    DELIVERED   = "delivered"      # reached at least one channel
    MUTED       = "muted"          # inside a maintenance window, not sent
    UNDELIVERED = "undelivered"    # attempted, reached nobody


#: The alert kinds whose delivery is what StateManager.alert_pending tracks: the
#: status notifications that announce a confirmed change. See _alert_unless_muted for why this is
#: an allow-list and why nothing else may write the flag.
_STATUS_ALERT_KINDS: frozenset[AlertKind] = frozenset({
    AlertKind.FAILURE, AlertKind.RECOVERY, AlertKind.PERFORMANCE_RECOVERY, AlertKind.DEGRADED,
    AlertKind.UNANNOUNCED_OUTAGE,
})


async def _alert_unless_muted(
    session      : aiohttp.ClientSession,
    url          : str,
    status_detail: str,
    kind         : AlertKind,
    maintenance  : list[dict] | None,
    state        : StateManager | None = None,
) -> AlertOutcome:
    """
    Send an alert unless this target is inside a maintenance window.

    Every alert in this module goes through here, and that is the point. The
    dispatch sites are five and growing; a mute checked at each of them is a
    guarantee somebody has to remember, and this project has decided more than
    once that a remembered guarantee is a broken one. One funnel means a new
    alert kind inherits the behaviour instead of opting out of it by omission.

    MUTED and DELIVERED both mean "nothing is owed", so neither arms the
    retry: queueing suppressed alerts would fire them all the moment the
    window closes, converting planned quiet into an unplanned storm. They
    differ on whether the caller may treat the alert as communicated, which is
    the distinction the markers depend on.

    Who may write alert_pending is decided HERE, by kind, and the list is
    _STATUS_ALERT_KINDS. That flag is one boolean per URL and the status setters
    read it as "the alert that announced this STATE change reached nobody".
    Every other kind answers a different question with its own marker, and
    letting them record into it fails in both directions: a lost certificate
    alert re-sends a RECOVERY the target had no new reason for, and a delivered
    one (or a trend, or a reminder) erases the record that a RECOVERY, DEGRADED
    or FAILURE never arrived -- silently, and for good, because the status has
    not changed so no transition ever fires again. It is a list of what MAY
    record rather than a list of what may not, on purpose: a kind added next
    year is read-only until somebody decides otherwise, instead of writing a
    flag nobody thought about.

    An advisory alert that reaches nobody is still not forgotten. It is counted
    per kind in state.undelivered_advisory, which is what keeps the exit code
    non-zero for a round in which a certificate, trend or reminder alert was
    lost: that signal used to exist only because the shared flag leaked it.
    """
    window = in_maintenance(maintenance, datetime.now(timezone.utc))
    if window is not None:
        # Logged, never swallowed. "Silenced on purpose" and "never noticed"
        # are different events, and only the log can tell them apart later.
        logger.info(
            "🔇  Alert suppressed by maintenance window (%s–%s UTC)  "
            "%s | %s. event_type=metric metric=alerts_muted_total value=1",
            window["start"], window["end"], url, status_detail,
        )
        # Gives back only the flag THIS transition armed (the setter writes it
        # with the transition, before anyone knows the alert will be muted), and
        # leaves an older one alone. If a previous delivery genuinely failed,
        # that flag is the only memory that nobody was ever told; a mute landing
        # on the retry would clear it and the outage would never be announced,
        # not even after the window closes — because by then the status has not
        # changed, so no transition fires. Silencing a NEW alert is the feature;
        # erasing the record of an OLD one that never arrived is data loss.
        if state is not None and kind in _STATUS_ALERT_KINDS:
            await state.release_muted_status_alert(url)
        return AlertOutcome.MUTED

    delivered = await dispatch_alert(
        session, url, status_detail=status_detail, kind=kind)
    if state is not None:
        if kind in _STATUS_ALERT_KINDS:
            await state.record_alert_outcome(url, delivered)
        elif not delivered:
            state.note_undelivered_advisory(kind)
    return AlertOutcome.DELIVERED if delivered else AlertOutcome.UNDELIVERED


async def _announce_if_owed(
    session    : aiohttp.ClientSession,
    url        : str,
    detail     : str,
    kind       : AlertKind,
    maintenance: list[dict] | None,
    state      : StateManager,
) -> None:
    """
    Announce a FAILURE/DEGRADED that a maintenance window muted, once it closes.

    The window silences the alert, not the probe, so a target that went DOWN
    inside it has its transition muted. If it is still DOWN afterwards no new
    transition ever fires, and with `remind` off nothing else would say so:
    the outage stays unannounced for as long as it lasts. check_url records
    the muted transition in muted_alert_owed; this sends the CURRENT state
    (the caller passes the kind that matches it) the first run the window is
    over, and clears the marker only once that send is confirmed.

    The window is checked here as well as inside the funnel, on purpose: while
    it is still open the marker must stay quietly owed, not log a suppression
    line every minute for the length of the window.
    """
    if not state.muted_alert_owed(url):
        return
    if in_maintenance(maintenance, datetime.now(timezone.utc)) is not None:
        return
    logger.warning(
        "📣  Announcing %s muted by a maintenance window  %s | %s. "
        "event_type=metric metric=muted_alerts_announced_total value=1",
        kind.name, url, detail,
    )
    outcome = await _alert_unless_muted(
        session, url, detail, kind, maintenance, state)
    # MUTED here means the window opened between the two clock reads: still owed.
    if outcome is not AlertOutcome.MUTED:
        await state.record_muted_alert(url, owed=False)


async def check_url(
    url                     : str,
    session                 : aiohttp.ClientSession,
    state                   : StateManager,
    expect_substring        : str   | None = None,
    expected_status         : int   | None = None,
    timeout_s               : float | None = None,
    degraded_ttfb_ms        : float | None = None,
    degraded_rtt_ms         : float | None = None,
    degraded_backend_ms     : float | None = None,
    expect_substring_display: str   | None = None,
    remind                  : bool  = False,
    maintenance             : list[dict] | None = None,
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
    effective_backend_slack_ms = (
        degraded_backend_ms if degraded_backend_ms is not None
        else TTFB_BACKEND_SLACK_MS
    )

    # Sample the path out-of-band before the probe: a raw TCP connect for RTT
    # and (for HTTPS) a controlled TLS handshake for TLS time and ALPN/h2.
    # aiohttp's own trace hooks fuse TCP connect and TLS and only ever offer
    # http/1.1, so they can give neither a clean RTT/TLS split nor server h2
    # support. A None sample (firewall, refusal) only degrades diagnostics —
    # never the probe.
    #
    # Inside its own try, and that is not decoration: this runs BEFORE the try
    # below, so anything it raised left check_url as an unhandled exception.
    # run_health_checks counts that as an internal error, withholds the
    # heartbeat and exits 1 -- every round, for as long as the entry stays in
    # the list. A YAML-loaded "http://host:99999/" (ValueError) or
    # "https://host..com/" (UnicodeError, a ValueError and not an OSError) is
    # all it takes. A sample that cannot be taken is not a reason to skip the
    # probe, so these specific exceptions mean "no sample" and nothing else; a
    # bug of any other kind stays loud.
    sample: ConnectionSample | None = None
    try:
        host_port = host_port_from_url(url)
        if host_port is not None:
            host, port = host_port
            sample = await sample_connection(
                host, port, use_tls=url.lower().startswith("https"))
    except (ValueError, UnicodeError, OSError, asyncio.TimeoutError) as exc:
        sample = None
        logger.warning(
            "Connection sample skipped for %s (%s: %s) — diagnostics degrade "
            "to the probe's own timings; the probe itself is unaffected. "
            "event_type=metric metric=connection_samples_failed_total value=1",
            url, type(exc).__name__, exc,
        )

    try:
        phases = await _probe_with_backoff(
            session, url, sample, expect_substring,
            effective_expected_status, effective_timeout_s,
            expect_substring_display=expect_substring_display,
        )
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        findings = analyze(
            phases,
            rtt_high_ms=effective_rtt_high_ms,
            backend_slack_ms=effective_backend_slack_ms,
        )
        _log_diagnostics(url, phases, findings)
        diagnostics = phases_to_dict(phases, findings)

        reason = degraded_reason(phases, findings, degraded_ttfb_ms=effective_degraded_ttfb_ms)
        if reason is not None:
            transitioned = await state.set_degraded(url, reason, diagnostics=diagnostics, stabilize=True)
            if transitioned:
                logger.warning("🟡  STATE CHANGE → DEGRADED  %s | %s", url, reason)
                kind, detail = state.status_alert(url, AlertKind.DEGRADED, reason)
                outcome = await _alert_unless_muted(
                    session, url, detail, kind, maintenance, state
                )
                # The only writer of "a transition was muted": see
                # _announce_if_owed for what reads it.
                await state.record_muted_alert(
                    url, owed=outcome is AlertOutcome.MUTED)
            else:
                logger.info("🟡  Still DEGRADED (alert suppressed)  %s | %s", url, reason)
                await _announce_if_owed(
                    session, url, reason, AlertKind.DEGRADED, maintenance, state)
            # After the status write: the record must exist and _carry_forward
            # must have run, or the stored threshold would be clobbered by the
            # very write we just did.
            await _maybe_alert_cert_expiry(session, url, state, phases, maintenance)
            return CheckOutcome(
                url=url, status=STATUS_DEGRADED, error=reason, checked_at=checked_at,
                phases=phases, findings=findings,
            )
        else:
            recovery_kind = state.recovery_kind(url)
            transitioned = await state.set_up(url, diagnostics=diagnostics, stabilize=True)
            if transitioned:
                logger.info("🟢  STATE CHANGE → UP    %s", url)
                kind, detail = state.status_alert(
                    url, recovery_kind,
                    "Performance is normal" if recovery_kind is AlertKind.PERFORMANCE_RECOVERY
                    else "Service is UP")
                await _alert_unless_muted(session, url, detail, kind, maintenance, state)
            else:
                logger.info("✅  OK (no change)       %s", url)
            await _maybe_alert_cert_expiry(session, url, state, phases, maintenance)
            return CheckOutcome(
                url=url, status=STATUS_UP, error=None, checked_at=checked_at,
                phases=phases, findings=findings,
            )

    except _ProbeFailure as exc:
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        transitioned = await state.set_down(url, error=exc.detail, stabilize=True)
        if transitioned:
            logger.error("🔴  STATE CHANGE → DOWN  %s | %s", url, exc.detail)
            outcome = await _alert_unless_muted(
                session, url, exc.detail, AlertKind.FAILURE, maintenance, state
            )
            await state.record_muted_alert(
                url, owed=outcome is AlertOutcome.MUTED)
        else:
            # Covers two cases: already-confirmed DOWN (repeat, alert already
            # sent), or a healthy target still inside its DOWN_CONFIRMATIONS
            # hysteresis window (not yet alerted). Either way: no alert here.
            logger.warning(
                "⚠️   Failure recorded, alert suppressed (already DOWN or "
                "pending confirmation)  %s | %s", url, exc.detail,
            )
            # Opt-in, and only while confirmed DOWN. Alerting once is right
            # against noise and wrong against a six-hour outage: after the
            # first message, silence and "resolved" look identical. DEGRADED is
            # deliberately excluded — this project treats it as a performance
            # signal rather than an availability one (uptime counts DEGRADED as
            # up), and a slow endpoint is not an incident to page about again.
            reminder_attempted = False
            if remind:
                # Checked here as well as inside the funnel, on purpose. The
                # funnel decides whether to SEND; this decides whether to
                # CONSUME the reminder, and those are different questions with
                # different costs. claim_reminder advances remind_step, and the
                # schedule never offers a consumed step again -- so a reminder
                # claimed during a window is one the outage loses for good, and
                # an outage that spans a freeze goes quiet for the rest of its
                # life. The duplicate check can only suppress, never let an
                # alert escape: the funnel still mutes if the two disagree.
                muted_now = in_maintenance(
                    maintenance, datetime.now(timezone.utc)) is not None
                claim = None if muted_now else await state.claim_reminder_step(url)
                if claim is not None:
                    reminder_attempted = True
                    detail = (
                        f"{exc.detail} — down for {claim.elapsed_h:.1f}h "
                        f"and still unresolved."
                    )
                    logger.error(
                        "⏳  REMINDER  %s | %.1fh down | %s",
                        url, claim.elapsed_h, exc.detail,
                    )
                    outcome = await _alert_unless_muted(
                        session, url, detail,
                        AlertKind.STILL_DOWN, maintenance, state,
                    )
                    # The claim above advanced remind_step before anyone knew the
                    # reminder would arrive. Anything short of DELIVERED hands
                    # the step back, so the next run offers this same reminder
                    # again as a STILL_DOWN. Without it the step stays consumed:
                    # the schedule never offers it twice, and the outage goes
                    # quiet until the next step -- a full day once the schedule
                    # is past 12 hours -- with the on-call believing the
                    # reminders are still coming.
                    #
                    # MUTED is returned too, not only UNDELIVERED. muted_now above
                    # keeps a claim from landing inside a window, so reaching
                    # MUTED here means the window opened between the two clock
                    # reads -- the same "consumed an alert nobody received" case
                    # by a narrower door.
                    if outcome is not AlertOutcome.DELIVERED:
                        await state.release_reminder(url, claim)
                    elif state.muted_alert_owed(url):
                        # The reminder says "down for Nh and still unresolved":
                        # a delivered one IS the announcement the window
                        # swallowed. A FAILURE on top would page twice for one
                        # event.
                        await state.record_muted_alert(url, owed=False)
            # Only a confirmed DOWN: a healthy status held by hysteresis is not
            # an outage to announce. And not in a run that already attempted a
            # reminder -- see above.
            if not reminder_attempted and state.current_status(url) == STATUS_DOWN:
                await _announce_if_owed(
                    session, url, exc.detail, AlertKind.FAILURE, maintenance, state)
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

async def _send_heartbeat(down_count: int) -> None:
    """
    Ping the external dead-man's switch to say this run completed.

    Who watches the watchman: every other failure mode in this service is one
    it can report on itself. This one is not. If the process dies, the
    container crash-loops, or the host powers off, nobody is left to send an
    alert about it — and total silence looks exactly like "everything is
    fine". An external watchdog (healthchecks.io or equivalent) inverts that:
    it expects a ping on a schedule and alerts when one stops arriving.

    Two decisions worth being explicit about:

    **The ping fires on run COMPLETION, not on all-targets-healthy.** Targets
    being DOWN is this monitor's normal operating condition — the same
    reasoning that makes a bare `python main.py` exit 0 with targets down.
    Gating the heartbeat on target health would make a genuine outage ALSO
    trip the watchdog, paging "your monitor is dead" on top of the real
    incident, at the exact moment the on-call is least able to tell the two
    apart. This watches the monitor, not the targets.

    **A failed ping is not retried.** The next run is 60 seconds away and
    watchdog grace periods are measured in minutes, so a single missed ping
    self-heals long before it matters. Retrying would spend run budget on a
    signal that repairs itself — and the whole point of this function is to
    cost the run as close to nothing as possible.

    Never raises. A watchdog that is down must not be able to take the real
    monitoring with it — the same isolation boundary HistoryRecorder holds.
    """
    url = os.getenv("HEARTBEAT_URL")
    if not url:
        # Deliberately a WARNING and not silence. Leaving this unset is a
        # legitimate choice, but an unset variable and a forgotten one look
        # identical from inside the process -- and the thing it protects
        # against is the one failure this service cannot report on itself.
        # Saying so once per run costs a line and closes that gap.
        logger.warning(
            "HEARTBEAT_URL is not set — nothing is watching this monitor. If "
            "this process dies, crash-loops, or the host powers off, no alert "
            "will ever say so, because silence is what a dead monitor looks "
            "like. Register a check at healthchecks.io (or equivalent) and put "
            "its ping URL in .env. event_type=metric "
            "metric=heartbeat_unconfigured_total value=1",
        )
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"down_count": down_count},
                timeout=aiohttp.ClientTimeout(total=HEARTBEAT_TIMEOUT_S),
            ) as resp:
                if resp.status < 400:
                    logger.info(
                        "Heartbeat sent. event_type=metric "
                        "metric=heartbeat_sent_total value=1"
                    )
                    return
                logger.warning(
                    "Heartbeat returned HTTP %s — the dead-man's switch may "
                    "fire even though this run completed. event_type=metric "
                    "metric=heartbeat_failures_total value=1 reason=http_%s",
                    resp.status, resp.status,
                )
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        logger.warning(
            "Heartbeat failed (%s: %s) — the dead-man's switch may fire even "
            "though this run completed. event_type=metric "
            "metric=heartbeat_failures_total value=1 reason=%s",
            type(exc).__name__, exc, type(exc).__name__,
        )


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


@dataclass
class RunReport:
    """
    What a run knows about itself that its return value (the DOWN count) does
    not carry, filled in by run_health_checks for whoever decides the exit code.

    A separate object instead of a wider return type because the int is read by
    every existing caller and test, and because these facts have a different
    audience: the DOWN count is about the targets, this is about the monitor.
    """

    #: Advisory alerts (certificate, trend, reminder) that reached nobody this
    #: run, by AlertKind value. Status alerts are not here: their record is
    #: alert_pending in state.json, which is also what retries them.
    undelivered_by_kind: dict[str, int] = field(default_factory=dict)

    #: Probes that raised something check_url did not anticipate. Not a target
    #: that is DOWN -- that is an answer. This is a check that produced none.
    internal_errors: int = 0

    #: State writes that failed (StateManager.write_failures).
    state_write_failures: int = 0

    @property
    def incomplete(self) -> bool:
        """
        True when the run cannot vouch for itself: a check crashed, or what it
        learned could not be saved. A DOWN target is not here, on purpose -- the
        heartbeat watches the monitor, not its targets.
        """
        return self.internal_errors > 0 or self.state_write_failures > 0


async def run_health_checks(
    targets     : list[str | Target] | None = None,
    state_path  : Path = STATE_FILE,
    history_path: Path = HISTORY_DB_FILE,
    report      : RunReport | None = None,
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
        report:       Optional RunReport to fill in with what the run knows
                      about itself (see RunReport). The caller that decides
                      the exit code passes one; direct callers need not.

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

    # A channel that failed for the last target is not going to work for
    # the next one within the same run, but each run starts believing it
    # might -- otherwise a single bad minute would mute a channel forever.
    reset_circuits()
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
    up_count        = 0
    degraded_count  = 0
    down_count      = 0
    internal_errors = 0
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
                degraded_backend_ms=t.degraded_backend_ms,
                remind=t.remind,
                maintenance=t.maintenance,
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
                #
                # It also counts as an internal error, which down_count cannot
                # say: a DOWN target is an answer the monitor got, a crashed
                # check is an answer it did not. Only the second means this
                # round cannot vouch for itself (see the heartbeat below).
                logger.critical(
                    "Unhandled exception for %s: %r — this is a bug, please report it.",
                    target.url, result,
                )
                down_count += 1
                internal_errors += 1
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

    # After the history write, because the trend reads what was just recorded.
    # This is the one alert that depends on history.db, which is why it cannot
    # live with the others inside the session block above -- and why a history
    # failure costs this alert and nothing else: the status alerts for this run
    # were all dispatched before this line.
    await _maybe_alert_trend(resolved, state, history_path)

    # The configured list is authoritative, and this is the only place that
    # holds both it and the state. Records for targets that are gone are not
    # harmless leftovers: stale_seconds() takes the OLDEST of them, so one
    # abandoned entry keeps the freshness banner red forever.
    await state.prune([t.url for t in resolved])

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

    # Every alert and every state write of the run has happened by now, the
    # trend's and the prune's included, so these tallies are final.
    if report is not None:
        report.undelivered_by_kind  = dict(state.undelivered_advisory)
        report.internal_errors      = internal_errors
        report.state_write_failures = state.write_failures

    # Last thing in the run, on purpose: everything that makes this a COMPLETE
    # cycle -- probes, alerts, state, history -- has already happened. Pinging
    # any earlier would tell the watchdog "alive and well" about a cycle that
    # had not finished, which is the one lie a dead-man's switch must never be
    # able to tell.
    #
    # "Finished" and "finished cleanly" are different, and the ping asserts the
    # second. A check that crashed left a target unwatched this minute; a state
    # write that failed leaves the next round starting from nothing. Either way
    # the watchdog would hear "alive and well" from a monitor that is neither,
    # and nothing outside this process could contradict it. So the success ping
    # is withheld and the watchdog's own grace period does the paging -- no
    # /fail call, the protocol is unchanged. A DOWN target does not land here:
    # it is this monitor's normal operating condition, and gating the ping on it
    # would page "your monitor is dead" on top of the real incident.
    #
    # A third reason to withhold: a round in which an alert reached nobody. A
    # rotated webhook answering 401 leaves the probes, the state and the history
    # all healthy while every FAILURE goes nowhere, and the exit code already
    # says so (_finish_run). The ping asserts "this monitor is doing its job",
    # and a monitor that cannot tell anyone is not -- sending it anyway kept the
    # watchdog green for as long as the webhook stayed dead. The count is the
    # one _finish_run uses (_alerts_undelivered), so the two cannot disagree. It
    # includes a status alert left over from an earlier round that this round's
    # retry also failed to deliver: still owed means still not working. A muted
    # alert is not here -- a maintenance window is a decision, not a failure --
    # and neither is a DOWN target whose alert was delivered.
    undelivered = _alerts_undelivered(state_path, state.undelivered_advisory)
    if internal_errors > 0 or state.write_failures > 0:
        logger.error(
            "Round INCOMPLETE: internal_errors=%d state_write_failures=%d. "
            "This run cannot vouch for itself: the success heartbeat is "
            "withheld and the process will exit non-zero even without "
            "--strict. event_type=metric metric=rounds_incomplete_total "
            "value=1",
            internal_errors, state.write_failures,
        )
    elif undelivered > 0:
        logger.error(
            "Heartbeat withheld: %d alert(s) reached NOBODY on any channel "
            "this round (or are still owed from an earlier one), so this "
            "monitor cannot tell anyone anything is wrong. The watchdog's "
            "grace period will page; the process exits non-zero even without "
            "--strict. event_type=metric metric=heartbeats_withheld_total "
            "value=1",
            undelivered,
        )
    else:
        await _send_heartbeat(down_count)

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
    # Widen the URL column to fit the longest target rather than a fixed 40:
    # a longer URL used to push every numeric column out of alignment, and a
    # misaligned table is read by eye, so the numbers stop lining up exactly
    # where someone is trying to compare them.
    url_width = max([len(t.url) for t in targets] + [len("Target")]) + 2
    header = (
        "Target".ljust(url_width)
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
        row = target.url.ljust(url_width)
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

    trend_lines = _trend_lines(targets, history_path, now)
    if trend_lines:
        lines.append("")
        lines.extend(trend_lines)

    return "\n".join(lines)


#: Window compared against the one before it. Half the default retention, so
#: both halves fit inside what was actually kept — comparing a full window
#: against one that retention already truncated would report a change that is
#: an artefact of deletion, not of the service.
TREND_WINDOW_DAYS: int = 7


def _trend_lines(
    targets     : list[Target],
    history_path: Path,
    now         : datetime | None = None,
) -> list[str]:
    """
    The "is this getting worse" section of --report.

    Every target gets a line, including the ones with nothing to say. A section
    that lists only problems cannot be told apart from a section that failed to
    run, and this project treats "silence that looks like health" as the thing
    to design against. So a target whose change was too small to matter says
    so, and prints the number that was too small.
    """
    rows: list[tuple[str, str]] = []
    for target in targets:
        try:
            recent, previous = latency_trend_windows(
                history_path, target.url, TREND_WINDOW_DAYS, now
            )
        except sqlite3.Error as exc:
            # The section says it could not be computed instead of vanishing:
            # a missing section reads as "nothing to report", the one message
            # this report must never send by accident. Every target reads the
            # same file, so one line covers all of them.
            logger.error(
                "Trend section unavailable: the history database could not be "
                "read (%s: %s).", type(exc).__name__, exc,
            )
            header = (
                f"p95 trend — last {TREND_WINDOW_DAYS}d against the previous "
                f"{TREND_WINDOW_DAYS}d"
            )
            return [
                header, "-" * len(header),
                f"unreadable: the history database could not be read "
                f"({type(exc).__name__}: {exc})",
            ]
        verdict = latency_trend(
            recent["p95_ttfb_ms"]   if recent   else None,
            previous["p95_ttfb_ms"] if previous else None,
            recent["samples"]       if recent   else 0,
            previous["samples"]     if previous else 0,
            # The denominators. Without them latency_trend cannot tell a slower
            # backend from a window that simply lost fewer probes.
            recent["total"]         if recent   else 0,
            previous["total"]       if previous else 0,
        )
        rows.append((target.url, _describe_trend(verdict)))

    if not rows:
        return []
    width  = max(len(url) for url, _ in rows) + 2
    header = (
        f"p95 trend — last {TREND_WINDOW_DAYS}d against the previous "
        f"{TREND_WINDOW_DAYS}d"
    )
    return [header, "-" * len(header)] + [
        url.ljust(width) + text for url, text in rows
    ]


def _describe_trend(t: dict) -> str:
    """One line per target, and never an empty one."""
    if t["verdict"] == TREND_NO_DATA:
        return "not enough data in one of the two windows"
    if t["verdict"] == TREND_TOO_FEW_SAMPLES:
        return (
            f"not enough samples to compare "
            f"({t['recent_samples']} vs {t['previous_samples']}, "
            f"{TREND_MIN_SAMPLES} needed)"
        )

    delta  = f"{t['change_pct']:+.0f}% ({t['change_ms']:+.0f}ms)"
    detail = f"{t['previous_p95_ms']:.0f}ms → {t['recent_p95_ms']:.0f}ms"

    if t["verdict"] == TREND_INCOMPARABLE:
        # Prints the change it refused to judge, and the rates that made it
        # refuse. "No comparable" without the numbers reads as a broken check,
        # which is the failure this whole section is written against.
        why = ("too few probes got a response"
               if t["suppressed_by"] == "success_floor"
               else "the two windows had different response rates")
        return (
            f"   not comparable  {why} "
            f"({t['previous_success_pct']:.0f}% → {t['recent_success_pct']:.0f}% "
            f"of probes got a response)   {delta}   {detail}"
        )

    if t["verdict"] == TREND_DEGRADING:
        return f"⚠  DEGRADING   {delta}   {detail}"
    if t["verdict"] == TREND_IMPROVING:
        return f"   improving    {delta}   {detail}"

    # Suppressed, and it names WHICH floor stopped it. "Measured and too small
    # to act on" must not read as "the check did not run" — that ambiguity is
    # what makes a quiet feature look like a broken one.
    floor = {
        "relative": f"change under the {TREND_MIN_CHANGE_PCT:.0f}% minimum",
        "absolute": f"change under the {TREND_MIN_CHANGE_MS:.0f}ms minimum",
    }.get(t["suppressed_by"], "stable")
    return f"   stable       {delta}   {detail}   [{floor}]"



def undelivered_alerts(state_path: Path) -> int:
    """
    How many targets are still owed an alert nobody received.

    Read from the persisted state rather than counted in memory during the
    run, deliberately: `alert_pending` is what actually drives the retry on
    the next cycle, so counting the same field keeps the exit code and the
    retry from ever disagreeing about how many alerts were lost. An in-memory
    tally could drift from the file; this cannot.

    Never raises — a missing or malformed state file means "nothing known to
    be pending", which is the same answer a fresh deployment gives.
    """
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    targets = raw.get("targets", raw) if isinstance(raw, dict) else {}
    if not isinstance(targets, dict):
        return 0
    return sum(
        1 for record in targets.values()
        if isinstance(record, dict) and record.get("alert_pending")
    )


def _alerts_undelivered(state_path: Path, advisory: dict[str, int]) -> int:
    """
    How many alerts this round could not deliver: status alerts still owed in
    state.json plus the advisory ones (certificate, trend, reminder) it lost.

    The one definition of "undelivered this round". The exit code
    (_finish_run) and the success heartbeat (run_health_checks) both read it,
    because they used to count differently -- the exit code saw a dead webhook
    and the heartbeat did not -- and a supervisor and a watchdog that disagree
    about whether the monitor works are worse than either alone.
    """
    return undelivered_alerts(state_path) + sum(advisory.values())


def _exit_code(
    strict     : bool,
    down_count : int,
    undelivered: int  = 0,
    incomplete : bool = False,
) -> int:
    """
    Compute the process exit code for a completed run.

    Two different questions, and only one of them is about the targets.

    "Some targets are down" is this monitor's normal operating condition, not
    a bug in the monitor — a bare `python main.py` must not fail a cron job
    because a target it watches is having a bad day. That stays behind
    --strict, unchanged.

    "An alert was generated and reached nobody" is the opposite: it is a
    failure OF the monitor, and it is invisible by construction, since the
    channel that would report it is the one that broke. It exits non-zero
    regardless of --strict, so the supervisor around this process — the
    compose loop's `|| exit 1`, a CronJob's failure count — can see a run
    where nothing was delivered instead of reading it as healthy.

    A third, same side of the line: a round that did not complete cleanly -- a
    check crashed, or state could not be saved -- is also a failure OF the
    monitor, and it exits non-zero regardless of --strict for the same reason.
    Its exit used to be 0, which is how a monitor with no state.json on disk
    reported itself healthy.
    """
    if undelivered > 0 or incomplete:
        return 1
    return 1 if (strict and down_count > 0) else 0


def _finish_run(
    strict    : bool,
    down_count: int,
    state_path: Path,
    report    : RunReport | None = None,
) -> int:
    """
    Turn a completed run into its exit code, and say why in the one line the
    on-call reads first.

    Lives outside the __main__ block so the decision that matters most to the
    supervisor is exercised by tests instead of by a deploy.

    Two sources of "an alert reached nobody", counted together because the
    supervisor cannot act on them differently. A state-change alert (FAILURE,
    RECOVERY, DEGRADED) is read from state.json: alert_pending is what retries
    it, so counting the same field keeps the exit code and the retry from
    disagreeing. A certificate, trend or reminder alert is read from the
    round's own tally, because it deliberately does not touch that flag -- and
    losing it must still show, since the exit code is the only place a dead
    webhook is visible from outside the process. The line names the kind: "an
    alert" is a count, "still_down=1" is where to look at 1 AM.

    An incomplete round (RunReport.incomplete) turns the exit code non-zero
    too; see _exit_code.
    """
    advisory    = report.undelivered_by_kind if report is not None else {}
    undelivered = _alerts_undelivered(state_path, advisory)
    incomplete  = report.incomplete if report is not None else False
    exit_code   = _exit_code(strict, down_count, undelivered, incomplete)
    if undelivered > 0:
        kinds = ", ".join(f"{k}={n}" for k, n in sorted(advisory.items())) or "none"
        logger.error(
            "Exiting %d: %d alert(s) reached NOBODY on any channel (advisory "
            "kinds lost this run: %s; the rest are state alerts still "
            "pending). They will be retried next run. event_type=metric "
            "metric=alerts_undelivered_total value=%d",
            exit_code, undelivered, kinds, undelivered,
        )
    elif strict and down_count > 0:
        logger.error(
            "--strict: exiting %d because %d target(s) are DOWN.",
            exit_code, down_count,
        )
    # An incomplete round has already said so, once, where the decision was made
    # (run_health_checks, "Round INCOMPLETE"); repeating it here would be two
    # lines for one condition.
    return exit_code


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------

def _report_main(history_path: Path = HISTORY_DB_FILE) -> int:
    """
    `--report`: print the table and return the exit code.

    The history readers re-raise a read error on purpose -- "no data" claims
    nothing was measured, and an unreadable database claims nothing at all --
    and nothing above them caught it, so a file that is not a SQLite database
    ended `--report` in a raw traceback. It now ends in one line naming the
    file and the reason, exit 1, and no table: a half-printed table over a
    database that could not be read would read as data.
    """
    try:
        report = _generate_report(load_targets(), history_path=history_path)
    except sqlite3.Error as exc:
        print(
            f"--report: the history database {history_path} cannot be read "
            f"({type(exc).__name__}: {exc}). Nothing to report until it is "
            f"restored or removed.",
            file=sys.stderr,
        )
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    if "--report" in sys.argv:
        # Mutually exclusive with a normal run: report and exit, probe
        # nothing. Reads history.db only — no session, no targets probed.
        sys.exit(_report_main())

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
    report     = RunReport()
    try:
        down_count = asyncio.run(
            run_health_checks(state_path=state_path, report=report))
    except KeyboardInterrupt:
        # Windows fallback — SIGINT arrives as KeyboardInterrupt, not via
        # add_signal_handler.  Log and exit cleanly without a traceback.
        logger.info("KeyboardInterrupt received — exiting.")
        sys.exit(0)

    sys.exit(_finish_run(strict, down_count, state_path, report))
