"""
history.py — SQLite-backed historical persistence for vigil-sre.

state.json answers "what is the status of this target RIGHT NOW, and should
an alert fire?". It is small, hot, and load-bearing — the flap-hysteresis
and alert-suppression logic in main.py depends on it being correct on every
single run. This module answers a different question: "what has this
target's status and latency looked like over time?" — the question that
unlocks percentiles, uptime %, and trend detection, none of which state.json
can ever answer because it only remembers the last observation.

Isolation boundary (the one decision that matters here)
---------------------------------------------------------
HistoryRecorder runs strictly AFTER StateManager has persisted state and
send_discord_alert has already fired for a run. Every method here catches
its own exceptions, logs them, and returns — never re-raises. A full disk,
a corrupted history.db, or a stuck SQLite lock degrades the history feature
(a few rows go unrecorded) and must never touch alert delivery. Losing an
alert is an incident; losing a history row is not.

Why sqlite3 (stdlib) + asyncio.to_thread instead of an async driver
--------------------------------------------------------------------
vigil-sre runs as a single instance (same constraint state.json already has).
sqlite3 is synchronous, but asyncio.to_thread() moves each blocking call off
the event loop for the ~150 microseconds it actually needs — cheap enough
that a dedicated async driver (aiosqlite) would add a dependency to buy back
a duration too small to matter.

Why the default journal mode, not WAL
--------------------------------------
An earlier version of this module set `PRAGMA journal_mode=WAL` for a future
read-only dashboard process to query history.db concurrently. WAL splits
persistence across history.db plus history.db-wal/-shm sidecar files, which
only merge back into the main file on an automatic or explicit checkpoint —
so most of the actual data can sit in the sidecars for extended periods. A
Docker deployment bind-mounting only history.db (the same single-file pattern
state.json already uses) would silently lose most of its history on every
container recreation, which is the exact failure this module exists to
prevent. That dashboard does not exist yet and has no committed design, so
this module does not pay WAL's deployment cost for it today. Revisit journal
mode if and when the dashboard's actual concurrency needs are known.

Python  : 3.11+
Depends : stdlib only (sqlite3, asyncio).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from diagnostics import Diagnosis, ProbePhases, bandwidth_confidence

logger = logging.getLogger("sre.history")

HISTORY_DB_FILE: Path = Path("history.db")
RETENTION_DAYS_DEFAULT: int = 30
CONNECT_TIMEOUT_S: float = 5.0  # how long to wait on a busy SQLite lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_results (
    id                   INTEGER PRIMARY KEY,
    run_started_at       TEXT NOT NULL,
    url                  TEXT NOT NULL,
    checked_at           TEXT NOT NULL,
    status               TEXT NOT NULL,
    error                TEXT,
    http_status          INTEGER,
    rtt_ms               REAL,
    dns_ms               REAL,
    connect_total_ms     REAL,
    tls_ms               REAL,
    ttfb_ms              REAL,
    server_processing_ms REAL,
    transfer_ms          REAL,
    body_bytes           INTEGER,
    goodput_bps          REAL,
    tls_cert_days_left   INTEGER,
    alpn_protocol        TEXT,
    h2_supported         INTEGER,
    connection_reused    INTEGER,
    bandwidth_confidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_results_url_time
    ON probe_results(url, checked_at);

CREATE TABLE IF NOT EXISTS probe_findings (
    probe_id INTEGER NOT NULL REFERENCES probe_results(id) ON DELETE CASCADE,
    code     TEXT NOT NULL,
    severity TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_findings_code ON probe_findings(code);
-- Without this, ON DELETE CASCADE full-scans probe_findings for every row
-- deleted from probe_results: measured 140s to prune one day's worth of
-- rows (8,640) versus 403ms with the index (348x). Steady-state prune (a
-- handful of rows per run) is cheap either way -- this only matters once,
-- on a large retroactive prune (e.g. lowering HISTORY_RETENTION_DAYS on an
-- established database), but IF NOT EXISTS means an existing database picks
-- this up on its very next connection with no migration step required.
CREATE INDEX IF NOT EXISTS idx_probe_findings_probe_id ON probe_findings(probe_id);
"""


def _retention_days_from_env() -> int:
    """
    Read HISTORY_RETENTION_DAYS from the environment, falling back to
    RETENTION_DAYS_DEFAULT on absence or a malformed value.

    A bad retention value must not crash the service — it isn't a primary
    data path the way an invalid targets.yaml entry is. Log a warning and
    keep running with the safe default instead.
    """
    raw = os.getenv("HISTORY_RETENTION_DAYS")
    if raw is None:
        return RETENTION_DAYS_DEFAULT
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "HISTORY_RETENTION_DAYS=%r is not an integer — using default %d.",
            raw, RETENTION_DAYS_DEFAULT,
        )
        return RETENTION_DAYS_DEFAULT
    if days <= 0:
        logger.warning(
            "HISTORY_RETENTION_DAYS=%d must be positive — using default %d.",
            days, RETENTION_DAYS_DEFAULT,
        )
        return RETENTION_DAYS_DEFAULT
    return days


@dataclass
class CheckOutcome:
    """
    Everything one check_url() run produced, for history persistence.

    phases is None and findings is empty on a totally failed probe — a
    target that never returned a successful response has nothing to measure
    beyond the fact that it was DOWN and why. status and error are always
    present regardless.

    checked_at is the wall-clock time check_url() actually finished probing
    THIS url — main.py sets it explicitly at each of its three return sites.
    It defaults to construction time here only so call sites that don't care
    about the exact value (most tests) don't need to pass one; that default
    is never reached in production, where the explicit value is always
    supplied.
    """

    url       : str
    status    : str                    # STATUS_UP | STATUS_DEGRADED | STATUS_DOWN
    error     : str | None = None      # None when clean UP; detail otherwise
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    phases    : ProbePhases | None = None
    findings  : list[Diagnosis] = field(default_factory=list)


class HistoryRecorder:
    """
    Append-only SQLite sink for probe history, isolated from the alert path.

    Construction never raises: if schema initialisation fails (e.g. the
    directory is not writable), the recorder marks itself disabled and every
    subsequent call becomes a silent no-op with a logged metric — history is
    a feature the service can run without, alerting is not.
    """

    def __init__(
        self,
        db_path       : Path = HISTORY_DB_FILE,
        retention_days: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._retention_days = (
            retention_days if retention_days is not None else _retention_days_from_env()
        )
        self._disabled = False
        try:
            self._init_schema()
        except sqlite3.Error as exc:
            logger.error(
                "History schema init failed for '%s' (%s) — history recording "
                "disabled for this run. event_type=metric "
                "metric=history_write_failures_total value=1 reason=schema_init",
                self._db_path, exc,
            )
            self._disabled = True

    # ------------------------------------------------------------------ I/O

    def _connect(self) -> sqlite3.Connection:
        # Default (rollback-journal) mode: single-file persistence, no -wal/
        # -shm sidecars. See the module docstring for why WAL was dropped.
        con = sqlite3.connect(self._db_path, timeout=CONNECT_TIMEOUT_S)
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def _record_run_sync(self, run_started_at: str, outcomes: list[CheckOutcome]) -> int:
        con = self._connect()
        try:
            written = 0
            for outcome in outcomes:
                phases = outcome.phases
                row = (
                    run_started_at,
                    outcome.url,
                    outcome.checked_at,
                    outcome.status,
                    outcome.error,
                    phases.http_status if phases else None,
                    phases.rtt_ms if phases else None,
                    phases.dns_ms if phases else None,
                    phases.connect_total_ms if phases else None,
                    phases.tls_ms if phases else None,
                    phases.ttfb_ms if phases else None,
                    phases.server_processing_ms if phases else None,
                    phases.transfer_ms if phases else None,
                    phases.body_bytes if phases else None,
                    phases.goodput_bps if phases else None,
                    phases.tls_cert_days_left if phases else None,
                    phases.alpn_protocol if phases else None,
                    (None if phases is None or phases.h2_supported is None
                     else int(phases.h2_supported)),
                    (None if phases is None else int(phases.connection_reused)),
                    bandwidth_confidence(phases) if phases else None,
                )
                cur = con.execute(
                    """
                    INSERT INTO probe_results (
                        run_started_at, url, checked_at, status, error,
                        http_status, rtt_ms, dns_ms, connect_total_ms, tls_ms,
                        ttfb_ms, server_processing_ms, transfer_ms, body_bytes,
                        goodput_bps, tls_cert_days_left, alpn_protocol,
                        h2_supported, connection_reused, bandwidth_confidence
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    row,
                )
                probe_id = cur.lastrowid
                for finding in outcome.findings:
                    con.execute(
                        "INSERT INTO probe_findings (probe_id, code, severity, evidence) "
                        "VALUES (?,?,?,?)",
                        (probe_id, finding.code, finding.severity, finding.evidence),
                    )
                written += 1
            con.commit()
            return written
        finally:
            con.close()

    def _prune_sync(self) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = self._connect()
        try:
            cur = con.execute("DELETE FROM probe_results WHERE checked_at < ?", (cutoff,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    # ------------------------------------------------------------------ Public API

    async def record_run(self, run_started_at: str, outcomes: list[CheckOutcome]) -> None:
        """
        Persist one run's outcomes in a single batch insert.

        Never raises. A write failure here is a history gap, not a service
        failure — the caller (run_health_checks) has already finished
        alerting by the time this is called, and must be able to proceed
        regardless of what happens in this method.
        """
        if self._disabled or not outcomes:
            return
        try:
            written = await asyncio.to_thread(self._record_run_sync, run_started_at, outcomes)
            logger.info(
                "History recorded %d row(s) for run %s. "
                "event_type=metric metric=history_rows_written_total value=%d",
                written, run_started_at, written,
            )
        except Exception as exc:  # noqa: BLE001 — deliberate: see module docstring
            logger.error(
                "History write failed for run %s: %s. event_type=metric "
                "metric=history_write_failures_total value=1 reason=write",
                run_started_at, exc,
            )

    async def prune(self) -> None:
        """
        Delete rows older than the configured retention window.

        Cheap in steady state: called once per run, so each call only ever
        deletes the handful of rows that just crossed the retention boundary
        since the previous call, not the whole aged backlog at once.
        """
        if self._disabled:
            return
        try:
            deleted = await asyncio.to_thread(self._prune_sync)
            if deleted:
                logger.info(
                    "History pruned %d row(s) older than %d days. "
                    "event_type=metric metric=history_rows_pruned_total value=%d",
                    deleted, self._retention_days, deleted,
                )
        except Exception as exc:  # noqa: BLE001 — deliberate: see module docstring
            logger.error(
                "History prune failed: %s. event_type=metric "
                "metric=history_write_failures_total value=1 reason=prune",
                exc,
            )


# --------------------------------------------------------------------- Reporting
#
# Read-only aggregates for the --report CLI flag. These are plain
# functions, not HistoryRecorder methods: they never write, so they don't
# need the disabled-on-init-failure state machine that guards the write
# path, and keeping them separate makes that boundary visible at a glance.
#
# Both open and close their own connection rather than reusing a shared one
# — --report is a one-shot CLI invocation, not a long-lived process, so
# there is no connection to amortise the cost across.

def _is_missing_schema(exc: sqlite3.OperationalError) -> bool:
    """
    True only for "no such table" — history.db has never been initialised.

    That is a real, expected sequence: --report on a freshly deployed
    instance, before the first probe cycle recorded anything. Every other
    OperationalError is a failure to READ, not an absence of data — most
    importantly "database is locked", which happens when --report runs while
    the probe loop is mid-write (reproduced in practice: the
    report showed "no data" for a target with 100% uptime and 30 days of
    rows). Collapsing the two turns "I could not read it" into the silent,
    confident claim "there is nothing there", and sends an operator hunting
    for a persistence bug in a healthy system.
    """
    return "no such table" in str(exc).lower()


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """
    Open history.db for reading and let the driver enforce that.

    sqlite3's URI mode=ro makes a write physically impossible rather than
    merely unintended — an INSERT on this connection fails with "attempt to
    write a readonly database". Both callers below are pure aggregates, and
    the api.py process runs alongside a live writer, so turning
    "should not write" into "cannot write" removes any path by which a reader
    could corrupt the writer's history.

    Existence is checked by the caller, not inferred from the error: mode=ro
    against a missing file raises "unable to open database file", and so does
    a permissions problem. The first is "no data yet"; the second is a real
    failure that must surface. Guessing between them from a string is how the
    locked-database bug happened.
    """
    return sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=CONNECT_TIMEOUT_S
    )


def uptime_pct(db_path: Path, url: str, since: str) -> float | None:
    """
    Percentage of *url*'s probes since *since* (an ISO-8601 UTC string,
    inclusive) that were not DOWN. Returns None when the window has zero
    rows for this target — the caller must render that as "no data", never
    as 0%, which would misreport a target nobody has probed yet as a target
    that was down for the entire window.

    DEGRADED counts as up. This mirrors the semantics already established
    everywhere else in this project: a DEGRADED alert is titled "Up But
    Hurting" (main.py's DiscordNotifier payload), and diagnostics.py treats
    DEGRADED as a performance signal, not an availability one. Uptime is the
    availability SLI; latency_percentiles() below is the performance SLI.
    Counting DEGRADED as down would make "slow but reachable" indistinguishable
    from "unreachable", collapsing two different signals into one number.

    Returns None (not a crash) when history.db has no schema yet — running
    --report before the first probe cycle has ever recorded anything is a
    real sequence (a freshly deployed instance, or an operator checking the
    report before the first `sleep 60` loop iteration), and "no data" is the
    honest answer, not a stack trace. Any OTHER read failure is raised: see
    _is_missing_schema for why the two must not look alike.
    """
    if not db_path.exists():
        return None  # nothing recorded yet -- see _connect_read_only
    con = _connect_read_only(db_path)
    try:
        row = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status != 'DOWN' THEN 1 ELSE 0 END) "
            "FROM probe_results WHERE url = ? AND checked_at >= ?",
            (url, since),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_schema(exc):
            return None  # history.db predates any recorded run
        logger.warning(
            "Could not read uptime history for %s: %s — reporting this as "
            "unavailable rather than as absent.", url, exc,
        )
        raise
    finally:
        con.close()
    total, up = row
    if not total:
        return None
    return (up / total) * 100.0


def latency_percentiles(db_path: Path, url: str, since: str) -> dict[str, float] | None:
    """
    p50/p95 TTFB (milliseconds) for *url* since *since*. Returns None when
    there are no rows with a non-NULL ttfb_ms in the window — a target that
    was DOWN for the entire window has nothing to measure, and a fabricated
    0ms reads as suspiciously fast rather than as absent.

    Uses CUME_DIST(), not PERCENT_RANK(), and takes the MIN of the rows that
    reach the threshold — that pair is the nearest-rank definition of a
    percentile, equal to the textbook value at index ceil(p * n).

    The distinction is not academic. PERCENT_RANK is (rank-1)/(n-1), so the
    highest row always evaluates to exactly 1.0 and `pr <= 0.95` structurally
    excludes the maximum — measured directly: with 10
    samples of 1..10ms it reported p95 = 9 instead of 10, and with 2 samples
    it reported the MINIMUM. The bias is systematic and always in the
    flattering direction, understating the latency tail in a tool whose only
    value is being trustworthy about numbers. CUME_DIST is rank/n, so the
    maximum reaches 1.0 and is included.

    Returns None (not a crash) when history.db has no schema yet — see
    uptime_pct()'s docstring for why that is a real, expected sequence, and
    _is_missing_schema for why every other read failure is raised instead.
    """
    if not db_path.exists():
        return None  # nothing recorded yet -- see _connect_read_only
    con = _connect_read_only(db_path)
    try:
        row = con.execute(
            """
            SELECT MIN(CASE WHEN cd >= 0.50 THEN ttfb_ms END) AS p50,
                   MIN(CASE WHEN cd >= 0.95 THEN ttfb_ms END) AS p95
            FROM (
                SELECT ttfb_ms, CUME_DIST() OVER (ORDER BY ttfb_ms) AS cd
                FROM probe_results
                WHERE url = ? AND checked_at >= ? AND ttfb_ms IS NOT NULL
            )
            """,
            (url, since),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_schema(exc):
            return None  # history.db predates any recorded run
        logger.warning(
            "Could not read latency history for %s: %s — reporting this as "
            "unavailable rather than as absent.", url, exc,
        )
        raise
    finally:
        con.close()
    p50, p95 = row
    if p50 is None:
        return None
    return {"p50_ttfb_ms": p50, "p95_ttfb_ms": p95}
