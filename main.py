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
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import aiohttp
import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap — load .env before anything reads os.getenv()
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Logging — dual handler (console + rotating-style flat file)
# ---------------------------------------------------------------------------
LOG_FORMAT  = "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("health_checker.log", mode="a", encoding="utf-8"),
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

STATUS_UP  : str = "UP"
STATUS_DOWN: str = "DOWN"


# ---------------------------------------------------------------------------
# YAML target loader
# ---------------------------------------------------------------------------

def load_targets(path: Path = TARGETS_FILE) -> list[str]:
    """
    Parse *path* as YAML and return the flat list of target URL strings.

    Expected structure::

        targets:
          - https://www.example.com
          - https://api.example.com/health

    Exits with a clear message if the file is missing, malformed, or empty.

    Args:
        path: Path to the YAML targets file.

    Returns:
        Non-empty list of URL strings.
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

    targets: list[str] = raw.get("targets", []) if isinstance(raw, dict) else []

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

class UrlState(TypedDict):
    """Persisted state record for a single monitored URL."""

    status      : str   # "UP" | "DOWN"
    last_checked: str   # ISO-8601 UTC — updated every run
    last_changed: str   # ISO-8601 UTC — updated only on a transition
    last_error  : str   # empty string when status is UP


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
        """Synchronous load called once at construction (before the loop starts)."""
        if not self._path.exists():
            logger.info("No state file found at '%s' — starting fresh.", self._path)
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read '%s' (%s) — starting with empty state.", self._path, exc
            )
            return {}

    def _write_sync(self) -> None:
        """Atomic write: tmp file → rename.  Called inside the lock."""
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
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

    async def set_up(self, url: str) -> bool:
        """
        Mark *url* as UP.

        Returns:
            True  — first check, or previous state was DOWN  → send alert.
            False — was already UP                           → suppress alert.
        """
        async with self._lock:
            now      = self._utc_now()
            previous = self._state.get(url)
            changed  = previous is None or previous["status"] != STATUS_UP

            self._state[url] = UrlState(
                status      =STATUS_UP,
                last_checked=now,
                last_changed=now if changed else previous["last_changed"],
                last_error  ="",
            )
            self._write_sync()
            return changed

    async def set_down(self, url: str, error: str) -> bool:
        """
        Mark *url* as DOWN.

        Returns:
            True  — first check, or previous state was UP   → send alert.
            False — was already DOWN                         → suppress alert.
        """
        async with self._lock:
            now      = self._utc_now()
            previous = self._state.get(url)
            changed  = previous is None or previous["status"] != STATUS_DOWN

            self._state[url] = UrlState(
                status      =STATUS_DOWN,
                last_checked=now,
                last_changed=now if changed else previous["last_changed"],
                last_error  =error,
            )
            self._write_sync()
            return changed


# ---------------------------------------------------------------------------
# Discord alerting  (async)
# ---------------------------------------------------------------------------

def _build_discord_payload(
    url          : str,
    status_detail: str,
    timestamp    : str,
    is_recovery  : bool,
) -> dict:
    """
    Construct a colour-coded Discord embed payload.

    Green embed for recoveries; red embed for failures.

    Args:
        url:           Monitored URL.
        status_detail: E.g. "HTTP 503", "Timeout (>5s)", or "Service is UP".
        timestamp:     ISO-8601 UTC string.
        is_recovery:   Determines embed colour and title.

    Returns:
        Dict ready to be JSON-serialised and POSTed to Discord.
    """
    if is_recovery:
        title        = "✅  Service Recovered"
        color        = 0x00C853  # green-A700
        status_label = "🟢  Current Status"
    else:
        title        = "🚨  Infrastructure Alert — Health Check Failed"
        color        = 0xFF0000  # red
        status_label = "📛  Failure Detail"

    return {
        "username"  : "SRE Health Checker",
        "avatar_url": "https://i.imgur.com/4M34hi2.png",
        "embeds": [
            {
                "title" : title,
                "color" : color,
                "fields": [
                    {
                        "name"  : "🌐  Target URL",
                        "value" : f"`{url}`",
                        "inline": False,
                    },
                    {
                        "name"  : status_label,
                        "value" : f"`{status_detail}`",
                        "inline": True,
                    },
                    {
                        "name"  : "🕐  Timestamp (UTC)",
                        "value" : f"`{timestamp}`",
                        "inline": True,
                    },
                ],
                "footer": {
                    "text": "Async SRE Health Monitor • alerts on state-change only"
                },
            }
        ],
    }


async def send_discord_alert(
    session      : aiohttp.ClientSession,
    url          : str,
    status_detail: str,
    is_recovery  : bool = False,
) -> None:
    """
    Dispatch a Discord Webhook POST using the shared aiohttp session.

    The function is a no-op (CRITICAL log) when DISCORD_WEBHOOK_URL is absent.
    It never raises — all network errors are caught and logged so a broken
    webhook never masks the underlying probe result.

    Args:
        session:       Shared aiohttp.ClientSession for the run.
        url:           Target whose state just changed.
        status_detail: Human-readable failure reason or "Service is UP".
        is_recovery:   True → green recovery embed.
    """
    webhook_url: str | None = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.critical(
            "DISCORD_WEBHOOK_URL is not set — alert suppressed for %s.", url
        )
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload   = _build_discord_payload(
        url=url, status_detail=status_detail, timestamp=timestamp,
        is_recovery=is_recovery,
    )

    try:
        async with session.post(
            webhook_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
        ) as resp:
            kind = "recovery" if is_recovery else "failure"
            if resp.status == 204:
                logger.info("Discord %s alert sent for %s.", kind, url)
            else:
                logger.error(
                    "Discord Webhook returned HTTP %s for %s alert on %s.",
                    resp.status, kind, url,
                )
    except asyncio.TimeoutError:
        logger.error("Timed out sending Discord alert for %s.", url)
    except aiohttp.ClientError as exc:
        logger.error("Discord alert failed for %s: %s", url, exc)


# ---------------------------------------------------------------------------
# Async probe with manual exponential-backoff retry loop
# ---------------------------------------------------------------------------

class _ProbeFailure(Exception):
    """Raised by _probe_once() on any non-200 response or network error."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def _probe_once(session: aiohttp.ClientSession, url: str) -> None:
    """
    Fire a single async HTTP GET against *url*.

    Args:
        session: Shared aiohttp.ClientSession.
        url:     Target URL.

    Raises:
        _ProbeFailure: On timeout, connection error, or non-200 response.
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    try:
        async with session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers=REQUEST_HEADERS,
        ) as resp:
            if resp.status != EXPECTED_STATUS:
                raise _ProbeFailure(f"HTTP {resp.status}")
            logger.info("  attempt OK — HTTP %s ← %s", resp.status, url)

    except asyncio.TimeoutError:
        raise _ProbeFailure(f"Timeout (>{REQUEST_TIMEOUT_S}s)")
    except aiohttp.ClientConnectionError as exc:
        raise _ProbeFailure(f"ConnectionError: {exc}")
    except aiohttp.ClientError as exc:
        raise _ProbeFailure(f"ClientError: {exc}")


async def _probe_with_backoff(session: aiohttp.ClientSession, url: str) -> None:
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
        session: Shared aiohttp.ClientSession.
        url:     Target URL.

    Raises:
        _ProbeFailure: After all RETRY_ATTEMPTS are exhausted.
    """
    last_exc: _ProbeFailure | None = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            await _probe_once(session, url)
            return  # success — exit immediately
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
# Per-URL orchestration coroutine
# ---------------------------------------------------------------------------

async def check_url(
    url    : str,
    session: aiohttp.ClientSession,
    state  : StateManager,
) -> None:
    """
    Run the full health-check pipeline for one URL.

    Pipeline
    --------
    1. _probe_with_backoff()  →  fires up to RETRY_ATTEMPTS async GETs.
    2. StateManager.set_up / set_down  →  persist result, detect transition.
    3. If transition detected  →  send_discord_alert()  via shared session.

    This coroutine is designed to be gathered concurrently with all others;
    it never touches shared mutable state outside the StateManager lock.

    Args:
        url:     Target URL to probe.
        session: Shared aiohttp.ClientSession for HTTP I/O.
        state:   Shared StateManager for transition detection and persistence.
    """
    logger.info("─── Checking: %s", url)

    try:
        await _probe_with_backoff(session, url)

        transitioned = await state.set_up(url)
        if transitioned:
            logger.info("🟢  STATE CHANGE → UP    %s", url)
            await send_discord_alert(
                session, url, status_detail="Service is UP", is_recovery=True
            )
        else:
            logger.info("✅  OK (no change)       %s", url)

    except _ProbeFailure as exc:
        transitioned = await state.set_down(url, error=exc.detail)
        if transitioned:
            logger.error("🔴  STATE CHANGE → DOWN  %s | %s", url, exc.detail)
            await send_discord_alert(
                session, url, status_detail=exc.detail, is_recovery=False
            )
        else:
            logger.warning("⚠️   Still DOWN (alert suppressed)  %s | %s", url, exc.detail)


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

async def run_health_checks(targets: list[str] | None = None) -> None:
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
        targets: Optional URL list override; defaults to loading targets.yaml.
    """
    urls  = targets or load_targets()
    state = StateManager()

    loop           = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    _install_signal_handlers(loop, shutdown_event)

    logger.info("=" * 64)
    logger.info(
        "SRE Health Checker v3 | targets=%d | concurrency=ALL | attempts=%d | backoff=%.0f–%.0fs",
        len(urls), RETRY_ATTEMPTS, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX,
    )
    logger.info("=" * 64)

    # One shared session for the whole run — efficient TCP reuse.
    connector = aiohttp.TCPConnector(ssl=True, limit=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Build one coroutine per target, then gather concurrently.
        tasks = [check_url(url, session, state) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log any unexpected exceptions that leaked past check_url's own handlers.
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                logger.critical(
                    "Unhandled exception for %s: %r — this is a bug, please report it.",
                    url, result,
                )

    if shutdown_event.is_set():
        logger.info("Shutdown signal was processed cleanly.")

    logger.info("=" * 64)
    logger.info("Run complete.  State file: %s", STATE_FILE.resolve())
    logger.info("=" * 64)


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(run_health_checks())
    except KeyboardInterrupt:
        # Windows fallback — SIGINT arrives as KeyboardInterrupt, not via
        # add_signal_handler.  Log and exit cleanly without a traceback.
        logger.info("KeyboardInterrupt received — exiting.")
        sys.exit(0)
