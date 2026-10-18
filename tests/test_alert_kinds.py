"""
tests/test_alert_kinds.py — an advisory alert must not touch the status-alert flag.

Why this is its own file
------------------------
`alert_pending` is one boolean per URL, and the status setters (set_up /
set_degraded / set_down, through _owed_an_alert) read it as "the alert that
announced this STATE change reached nobody". Every other alert kind --
certificate, trend, reminder -- is a different question with its own marker,
and each of them was also writing to that one boolean, in both directions:

  - a cert alert that FAILED armed it, so the next round re-sent a
    RECOVERY / DEGRADED / FAILURE the target never had a new reason for;
  - a cert / trend / reminder alert that was DELIVERED cleared it, so a
    RECOVERY / DEGRADED / FAILURE that never arrived was forgotten for good,
    with nothing in the log. The second one is the dangerous direction: it is
    the "mark before confirm" shape this project already fixed three times,
    through a shared flag instead of a shared write.

The reminder had a third leg: claim_reminder advances remind_step before
anyone knows the reminder arrived, and the shared flag "rescued" the lost
one by re-sending it as a FAILURE -- the wrong message, because the retry
path for a reminder was not a reminder.

The webhook here is a real aiohttp server on 127.0.0.1 that records the KIND
of every alert it receives, so these tests assert what a human would have been
told rather than which internal function was called.

Coverage:
  A. Round-trips through a webhook that fails and then recovers (the defect)
  B. The funnel's default: which kinds may write alert_pending
  C. A reminder that did not arrive is given back, not spent
  D. The exit code stays non-zero for an undelivered advisory alert
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import main
import notifiers
from diagnostics import ProbePhases
from history import HistoryRecorder
from notifiers import AlertKind

URL = "https://svc.example"

#: The title each alert carries -> the kind the test reads it as. A payload
#: that matches none of them is reported as "?<title>" so an unexpected alert
#: fails loudly instead of being silently ignored.
_TITLES = {
    "Service Recovered":   "RECOVERY",
    "Performance normal":  "PERFORMANCE_RECOVERY",
    "Health Check Failed": "FAILURE",
    "Still Down":          "STILL_DOWN",
    "Certificate Expiring": "CERT",
    "Getting Slower":      "TREND",
    "Service Degraded":    "DEGRADED",
}


def _kind_of(payload: dict) -> str:
    title = payload["embeds"][0]["title"] if "embeds" in payload else payload.get("text", "")
    for needle, kind in _TITLES.items():
        if needle in title:
            return kind
    return "?" + title


class Hook:
    """
    Local webhook (127.0.0.1 only) recording the kind of every request.

    `behavior(n, kind) -> status` decides the answer; n counts requests so far.
    204 is what Discord answers on success, 400 is a non-retryable rejection
    (one request per alert), 500 is retried by the notifier (three requests).
    """

    def __init__(self) -> None:
        self.reqs: list[str] = []
        self.behavior = lambda n, kind: 204
        self._n = 0
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def _handle(self, request: web.Request) -> web.Response:
        kind = _kind_of(await request.json())
        self._n += 1
        self.reqs.append(kind)
        return web.Response(status=self.behavior(self._n, kind))

    async def start(self) -> "Hook":
        app = web.Application()
        app.router.add_post("/discord/{token}", self._handle)
        self._runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/discord/NOT-A-REAL-TOKEN"

    def clear(self) -> None:
        self.reqs.clear()

    async def stop(self) -> None:
        assert self._runner is not None
        await self._runner.cleanup()


def _phases(cert_days: int | None = None, ttfb: float = 50.0) -> ProbePhases:
    return ProbePhases(
        url=URL, http_status=200, ttfb_ms=ttfb, transfer_ms=1.0,
        body_bytes=10, tls_cert_days_left=cert_days,
    )


def _probe_ok(monkeypatch, phases: ProbePhases) -> None:
    async def _probe(*a, **k):
        return phases
    monkeypatch.setattr(main, "_probe_with_backoff", _probe)


def _probe_fail(monkeypatch, detail: str = "HTTP 500 (expected 200)") -> None:
    async def _probe(*a, **k):
        raise main._ProbeFailure(detail)
    monkeypatch.setattr(main, "_probe_with_backoff", _probe)


@pytest.fixture
def real_writes() -> None:
    """Opt-in marker: the test reads state.json back, so writes must hit disk."""


@pytest.fixture(autouse=True)
def _state_stays_in_memory(request, monkeypatch) -> None:
    """
    Keep StateManager's atomic write off the disk unless a test asks for it.

    Every set_*/record_* call does write-tmp-then-rename, and on a Windows host
    with a real-time scanner on the temp directory each rename costs seconds --
    these tests make hundreds of them, so the file would add minutes to a suite
    that already takes several. Nothing asserted here depends on the file: the
    in-memory record is what the setters read back. The tests that DO read
    state.json (the round report, the end-to-end process) take `real_writes`.
    """
    if "real_writes" in request.fixturenames:
        return
    monkeypatch.setattr(main.StateManager, "_write_sync", lambda self: None)


@pytest.fixture
async def env(monkeypatch, tmp_path):
    hook = await Hook().start()
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", hook.url)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(notifiers, "WEBHOOK_RETRY_BASE_S", 0.01)

    async def _no_sample(*a, **k):
        return None
    monkeypatch.setattr(main, "sample_connection", _no_sample)
    notifiers.reset_circuits()
    state = main.StateManager(tmp_path / "state.json")
    yield hook, state, monkeypatch, tmp_path
    await hook.stop()


async def _round(state: main.StateManager, **kw) -> main.CheckOutcome:
    """One check_url, the way a run does it: circuits reset at the start."""
    notifiers.reset_circuits()
    async with aiohttp.ClientSession() as session:
        return await main.check_url(URL, session, state, **kw)


async def _healthy_and_told(state: main.StateManager) -> None:
    await state.set_up(URL)
    await state.record_alert_outcome(URL, True)


async def _down_and_told(state: main.StateManager, hours: float = 0.0) -> None:
    await state.set_down(URL, "boom")
    await state.record_alert_outcome(URL, True)
    if hours:
        started = datetime.now(timezone.utc) - timedelta(hours=hours)
        state._state[URL]["last_changed"] = started.strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# A. Round-trips through a webhook that fails and then recovers
# =============================================================================


async def test_a_cert_alert_that_failed_does_not_owe_a_status_alert(env) -> None:
    """Target UP and unchanged, cert alert rejected by the webhook. The only
    thing owed next round is the CERT retry. Before the fix the failure armed
    alert_pending, so round 2 also sent a RECOVERY for a target that never
    went down -- a false 'service recovered' page, at 03:00."""
    hook, state, mp, tmp = env
    await _healthy_and_told(state)
    _probe_ok(mp, _phases(cert_days=20))

    hook.behavior = lambda n, kind: 400
    await _round(state)
    assert hook.reqs == ["CERT"]

    hook.clear()
    hook.behavior = lambda n, kind: 204
    await _round(state)
    assert hook.reqs == ["CERT"], f"only the cert retry is owed, got {hook.reqs}"
    assert not state._state[URL].get("alert_pending")


async def test_a_delivered_cert_alert_does_not_erase_a_lost_recovery(env) -> None:
    """The dangerous direction. Target recovers and has a cert under 30 days;
    the webhook flaps so the RECOVERY (three attempts) is lost and the CERT
    that follows gets through. The CERT delivery used to write
    alert_pending=False, deleting the only record that RECOVERY never arrived:
    the on-call kept believing the service was down, and nothing retried."""
    hook, state, mp, tmp = env
    await _down_and_told(state)
    _probe_ok(mp, _phases(cert_days=20))

    hook.behavior = lambda n, kind: 500 if n <= 3 else 204
    await _round(state)
    assert hook.reqs[:3] == ["RECOVERY"] * 3 and hook.reqs[3:] == ["CERT"], hook.reqs

    hook.clear()
    await _round(state)
    assert "RECOVERY" in hook.reqs, (
        f"RECOVERY never reached anybody and nothing re-sent it; round 2 sent "
        f"{hook.reqs}")


async def test_a_delivered_cert_alert_does_not_erase_a_lost_degraded(env) -> None:
    """Same shape with DEGRADED: a slow target with an expiring certificate."""
    hook, state, mp, tmp = env
    await _healthy_and_told(state)
    _probe_ok(mp, _phases(cert_days=None, ttfb=5000.0))
    for _ in range(main.DEGRADED_CONFIRMATIONS - 1):
        await _round(state)
    _probe_ok(mp, _phases(cert_days=20, ttfb=5000.0))

    hook.behavior = lambda n, kind: 500 if n <= 3 else 204
    await _round(state)
    assert state.current_status(URL) == main.STATUS_DEGRADED
    assert hook.reqs[:3] == ["DEGRADED"] * 3 and hook.reqs[3:] == ["CERT"], hook.reqs

    hook.clear()
    await _round(state)
    assert "DEGRADED" in hook.reqs, (
        f"DEGRADED never reached anybody and nothing re-sent it; round 2 sent "
        f"{hook.reqs}")


async def test_a_delivered_trend_alert_does_not_erase_a_lost_down(env) -> None:
    """The trend step runs after every status alert of the run, and the webhook
    that dropped the FAILURE can be back by then. Its delivery used to clear
    the pending flag the FAILURE had just armed."""
    hook, state, mp, tmp = env
    db = tmp / "history.db"
    now = datetime.now(timezone.utc)

    def seed(days_ago: int, ttfb: float, n: int = 80) -> list[main.CheckOutcome]:
        return [
            main.CheckOutcome(
                url=URL, status="UP",
                checked_at=(now - timedelta(days=days_ago, minutes=i)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                phases=ProbePhases(url=URL, http_status=200, ttfb_ms=ttfb + (i % 3),
                                   transfer_ms=1.0, body_bytes=1),
            )
            for i in range(n)
        ]

    await HistoryRecorder(db).record_run(
        now.strftime("%Y-%m-%dT%H:%M:%SZ"), seed(10, 50.0) + seed(2, 380.0))

    await _healthy_and_told(state)
    _probe_fail(mp)
    hook.behavior = lambda n, kind: 500
    for _ in range(main.DOWN_CONFIRMATIONS):
        hook.clear()
        await _round(state)
    assert state.current_status(URL) == main.STATUS_DOWN
    assert "FAILURE" in hook.reqs
    assert state._state[URL]["alert_pending"] is True

    hook.clear()
    hook.behavior = lambda n, kind: 204
    await main._maybe_alert_trend([main.Target(url=URL, trend_alerts=True)], state, db)
    assert hook.reqs == ["TREND"], hook.reqs
    assert state._state[URL]["alert_pending"] is True, (
        "a delivered TREND alert cleared the pending FAILURE")


# =============================================================================
# B. The funnel's default
# =============================================================================


@pytest.mark.parametrize("kind", [
    AlertKind.FAILURE, AlertKind.RECOVERY, AlertKind.DEGRADED,
])
async def test_a_status_alert_records_its_outcome(tmp_path, monkeypatch, kind) -> None:
    """The three kinds whose delivery IS what alert_pending tracks."""
    async def _lost(*a, **k):
        return False
    monkeypatch.setattr(main, "dispatch_alert", _lost)
    state = main.StateManager(tmp_path / "state.json")
    await _healthy_and_told(state)

    await main._alert_unless_muted(None, URL, "detail", kind, None, state)
    assert state._state[URL]["alert_pending"] is True


@pytest.mark.parametrize("kind", [
    AlertKind.CERT_EXPIRING_WARN, AlertKind.CERT_EXPIRING_CRIT,
    AlertKind.STILL_DOWN, AlertKind.TREND_DEGRADING,
])
@pytest.mark.parametrize("delivered", [True, False])
async def test_an_advisory_alert_never_touches_alert_pending(
        tmp_path, monkeypatch, kind, delivered) -> None:
    """Either outcome, either starting value: the flag is the status alert's."""
    async def _dispatch(*a, **k):
        return delivered
    monkeypatch.setattr(main, "dispatch_alert", _dispatch)

    for pending_before in (True, False):
        state = main.StateManager(tmp_path / f"state-{pending_before}.json")
        await _healthy_and_told(state)
        await state.record_alert_outcome(URL, not pending_before)
        assert bool(state._state[URL].get("alert_pending")) is pending_before

        await main._alert_unless_muted(None, URL, "detail", kind, None, state)
        assert bool(state._state[URL].get("alert_pending")) is pending_before, (
            f"{kind} (delivered={delivered}) changed alert_pending")


async def test_a_kind_nobody_has_written_yet_cannot_erase_a_pending_alert(
        tmp_path, monkeypatch) -> None:
    """The safe default. The funnel lists the kinds that MAY record; anything
    else -- including a member added next year -- is read-only, so forgetting
    to opt it out cannot reproduce this defect."""
    async def _delivered(*a, **k):
        return True
    monkeypatch.setattr(main, "dispatch_alert", _delivered)
    state = main.StateManager(tmp_path / "state.json")
    await _healthy_and_told(state)
    await state.record_alert_outcome(URL, False)          # a status alert is owed

    class FutureKind(Enum):
        SOME_NEW_ALERT = "some_new_alert"

    await main._alert_unless_muted(
        None, URL, "detail", FutureKind.SOME_NEW_ALERT, None, state)  # type: ignore[arg-type]
    assert state._state[URL]["alert_pending"] is True


# =============================================================================
# C. A reminder that did not arrive is given back, not spent
# =============================================================================


async def test_a_failed_reminder_is_retried_as_a_reminder(env) -> None:
    """claim_reminder advances remind_step before anyone knows the reminder
    arrived. The shared flag used to paper over that by re-sending it as a
    FAILURE -- the wrong message, saying the outage had just started. The step
    is now handed back, so the retry is the STILL_DOWN it always was."""
    hook, state, mp, tmp = env
    await _down_and_told(state, hours=1.5)
    _probe_fail(mp)

    hook.behavior = lambda n, kind: 400
    await _round(state, remind=True)
    assert hook.reqs == ["STILL_DOWN"]

    hook.clear()
    hook.behavior = lambda n, kind: 204
    await _round(state, remind=True)
    assert hook.reqs == ["STILL_DOWN"], f"owed a reminder, sent {hook.reqs}"
    assert state._state[URL]["remind_step"] >= 1, "and now it is spent"

    hook.clear()
    await _round(state, remind=True)
    assert hook.reqs == [], "a delivered reminder is not repeated"


async def test_a_failed_reminder_then_recovery_sends_one_recovery(env) -> None:
    """Regression guard, green before the fix and it must stay green: an
    undelivered reminder followed by a recovery announces the recovery once and
    leaves nothing pending."""
    hook, state, mp, tmp = env
    await _down_and_told(state, hours=1.5)
    _probe_fail(mp)
    hook.behavior = lambda n, kind: 400
    await _round(state, remind=True)

    hook.clear()
    hook.behavior = lambda n, kind: 204
    _probe_ok(mp, _phases())
    await _round(state, remind=True)
    assert hook.reqs == ["RECOVERY"]
    assert not state._state[URL].get("alert_pending")

    hook.clear()
    await _round(state, remind=True)
    assert hook.reqs == []


async def test_releasing_a_reminder_is_a_noop_once_the_outage_is_over(tmp_path) -> None:
    """A recovery replaces the record, and remind_step is deliberately not
    carried forward. Handing a step back afterwards must not write a stale
    counter into the healthy record -- it would silence the first reminder of
    the NEXT outage."""
    state = main.StateManager(tmp_path / "state.json")
    await _down_and_told(state, hours=4)
    claim = await state.claim_reminder_step(URL)
    assert claim is not None and claim.previous_step == 0 and claim.claimed_step >= 1

    await state.set_up(URL)
    await state.release_reminder(URL, claim)
    assert "remind_step" not in state._state[URL]

    await state.set_down(URL, "again")
    await state.set_down(URL, "again")
    assert state._state[URL].get("remind_step", 0) == 0


async def test_releasing_a_reminder_restores_the_step_it_found(tmp_path) -> None:
    """Monitor back after six hours: three reminders were owed, one is sent.
    Giving that one back restores the step the claim FOUND, not claimed-1, so
    the bookkeeping is exactly what it was before the attempt."""
    state = main.StateManager(tmp_path / "state.json")
    await _down_and_told(state, hours=2.5)               # 1h and 2h steps owed
    first = await state.claim_reminder_step(URL)
    assert first is not None
    await state.release_reminder(URL, first)
    assert state._state[URL].get("remind_step", 0) == first.previous_step
    again = await state.claim_reminder_step(URL)
    assert again is not None and again.claimed_step == first.claimed_step


# =============================================================================
# D. The exit code stays non-zero for an undelivered advisory alert
# =============================================================================


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_main_with_a_reminder_that_reaches_nobody(tmp_path: Path) -> subprocess.CompletedProcess:
    """
    A real `python main.py`, cwd in a temp dir. One target already confirmed
    DOWN for two hours with `remind: true`, so the round owes a reminder; the
    webhook points at a closed port, so the reminder reaches nobody.

    Nothing here is `alert_pending`-specific: it asserts what a supervisor sees,
    which is the contract. That is why it can be written against the old and
    the new implementation alike.
    """
    work = tmp_path / "work"
    work.mkdir()
    dead = f"http://127.0.0.1:{_closed_port()}/x"
    (work / "targets.yaml").write_text(
        f"targets:\n  - url: {dead}\n    remind: true\n", encoding="utf-8")
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (work / "state.json").write_text(json.dumps({
        "schema_version": main.STATE_SCHEMA_VERSION,
        "targets": {dead: {
            "status": "DOWN", "last_checked": started, "last_changed": started,
            "last_error": "seeded", "consecutive_failures": 2, "remind_step": 0,
        }},
    }), encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DISCORD", "SLACK", "HEARTBEAT"))}
    env.update(
        PYTHONPATH=str(REPO),
        PYTHONIOENCODING="utf-8",
        DISCORD_WEBHOOK_URL=f"http://127.0.0.1:{_closed_port()}/never",
        STATE_FILE_PATH=str(work / "state.json"),
        TARGETS_STORE_PATH=str(work / "nostore.json"),
    )
    return subprocess.run(
        [sys.executable, str(REPO / "main.py")], cwd=work, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


@pytest.fixture(scope="module")
def reminder_nobody_got(tmp_path_factory) -> subprocess.CompletedProcess:
    """One real process shared by the two assertions below (about 10s)."""
    return _run_main_with_a_reminder_that_reaches_nobody(
        tmp_path_factory.mktemp("exit_e2e"))


def test_an_undelivered_reminder_still_makes_the_process_exit_non_zero(
        reminder_nobody_got) -> None:
    """Pins today's behaviour and must survive the fix. Today it holds by
    accident (the reminder armed the shared flag, and the flag is what the exit
    code counts); after the fix it holds because the round counts it itself.
    Without it, removing the shared flag would quietly make the supervisor see
    a clean run while nobody was told anything."""
    p = reminder_nobody_got
    assert p.returncode == 1, (
        f"a reminder reached nobody and the process exited {p.returncode}\n{p.stdout[-1500:]}")


def test_the_exit_log_names_the_kind_that_reached_nobody(reminder_nobody_got) -> None:
    """At 1 AM the line before the exit must say WHICH alert was lost, without
    opening state.json: 'an alert' is a count, 'still_down' is a place to look."""
    p = reminder_nobody_got
    exit_lines = [ln for ln in p.stdout.splitlines() if "Exiting" in ln]
    assert exit_lines, p.stdout[-1500:]
    assert "still_down" in exit_lines[-1], exit_lines[-1]
    assert "alerts_undelivered_total" in exit_lines[-1]


async def test_the_round_counts_an_undelivered_advisory_alert_by_kind(
        env, real_writes, caplog) -> None:
    """In-process twin of the e2e above, for the other advisory kinds: a cert
    alert that reaches nobody is counted in the round (not in state.json) and
    named in the exit log."""
    hook, state, mp, tmp = env
    _probe_ok(mp, _phases(cert_days=20))
    hook.behavior = lambda n, kind: 400 if kind == "CERT" else 204

    report = main.RunReport()
    await state.set_up(URL)
    await state.record_alert_outcome(URL, True)
    await main.run_health_checks(
        targets=[main.Target(url=URL)], state_path=tmp / "state.json",
        history_path=tmp / "history.db", report=report)

    assert report.undelivered_by_kind == {"cert_expiring_warn": 1}
    assert not main.undelivered_alerts(tmp / "state.json"), (
        "the advisory alert must not be counted through the status flag")

    with caplog.at_level("ERROR", logger="sre.health_checker"):
        code = main._finish_run(False, 0, tmp / "state.json", report)
    assert code == 1
    assert [r.getMessage() for r in caplog.records if "Exiting" in r.getMessage()
            and "cert_expiring_warn=1" in r.getMessage()], caplog.text
