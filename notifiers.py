"""
notifiers.py — Multi-channel alert delivery for vigil-sre.

Why more than one channel
--------------------------
A single webhook is a single point of delivery failure, and not on the side
you would expect. The POST can succeed, Discord can accept it, and the alert
can still fail to reach a human: buried under other notifications, delayed by
the phone's delivery queue, or silenced by a notification setting nobody
remembers configuring. No retry policy fixes that, because nothing failed —
delivery worked and attention did not.

Two independent channels receiving the same alert covers that. The
duplication is the point, not waste. There are no routing rules by design:
every channel receives every alert, because a rule that sends an alert to
only one place reintroduces exactly the single point of failure this module
exists to remove.

Isolation boundary
------------------
``send()`` never raises. ``dispatch_alert()`` gathers with
``return_exceptions=True`` on top of that — belt and braces on purpose: the
never-raise invariant is a convention a future notifier author can break
without noticing, and the gather makes it structural. A dead channel must
never reach check_url, and must never delay or block a healthy channel.

Why parallel dispatch is a requirement, not an optimisation
------------------------------------------------------------
Measured against this module's own constants: worst case per channel is
3 attempts x 5 s timeout + two 30 s capped Retry-After sleeps = 75 s.
Dispatching two channels sequentially is 150 s against a RUN_BUDGET_S of
60 s — a guaranteed overrun of 2.5x. In parallel it is max(75, 75) = 75 s,
which is what a single channel already costs today.

Python  : 3.11+
Depends : aiohttp (already required by the probe), stdlib otherwise.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
from datetime import datetime, timezone
from enum import Enum

import aiohttp

logger = logging.getLogger("sre.notifiers")

# Webhook delivery retry: alerts fire on state transitions ONLY, so a POST
# that dies on a transient 429/5xx has no second chance next run — the alert
# is gone. Retrying delivery here is the monitor's one job.
WEBHOOK_RETRY_ATTEMPTS   : int   = 3     # total attempts (1 original + 2 retries)
WEBHOOK_RETRY_BASE_S     : float = 1.0   # linear backoff: 1 s, then 2 s
WEBHOOK_RETRY_AFTER_CAP_S: float = 30.0  # cap on an honoured Retry-After value

# Deliberately independent from the probe's REQUEST_TIMEOUT_S even though both
# are 5 s today. They were coupled only by history; the
# probe timeout is already per-target configurable while this one stayed global, so they
# are different concerns that happened to share a number.
WEBHOOK_TIMEOUT_S: float = 5.0

# Circuit breaker, scoped to ONE run on purpose.
#
# main.py probes every target concurrently and exits; the compose loop starts
# it again 60 s later. So a breaker that persisted across runs would need to
# live in state.json, and the risk it guards against does not need that. The
# risk is inside a single run: with a channel down, every target pays the full
# retry policy independently -- 3 attempts x 5 s, plus capped Retry-After
# sleeps. Six targets absorb that inside RUN_BUDGET_S. Thirty do not, and the
# probe starts skipping cycles because the ALERT path is slow, which is the
# isolation boundary this project otherwise holds everywhere.
#
# After CIRCUIT_BREAK_AFTER consecutive failures on a channel within one run,
# the remaining alerts on that channel fail fast instead of re-proving what the
# run already established. The sibling channel is untouched: breaking is
# per-channel precisely so a dead Discord cannot slow down a healthy Slack.
CIRCUIT_BREAK_AFTER: int = 3

#: Consecutive failures per channel name, for the current run only.
_circuit_failures: dict[str, int] = {}


def reset_circuits() -> None:
    """Clear the breaker. Called once at the start of every run."""
    _circuit_failures.clear()


def _circuit_is_open(channel: str) -> bool:
    return _circuit_failures.get(channel, 0) >= CIRCUIT_BREAK_AFTER


def _record_circuit(channel: str, delivered: bool) -> None:
    if delivered:
        _circuit_failures[channel] = 0
    else:
        _circuit_failures[channel] = _circuit_failures.get(channel, 0) + 1


#: Placeholder written in place of a webhook URL that would otherwise reach a log.
REDACTED = "<webhook redacted>"


def _redact(text: str, webhook: str | None) -> str:
    """
    Strip *webhook* out of *text* before it reaches a log.

    aiohttp puts the FULL request URL into the message of its URL-shaped
    errors (InvalidUrlClientError, NonHttpUrlClientError). Reproduced during
    practice: a webhook whose scheme was typo'd — "htps://" —
    lands verbatim in health_checker.log, token included, and that token
    still works for anyone who reads the file and fixes the scheme. The log
    rotates at 10 MiB x 5 backups, so it is a live credential sitting on disk
    for a long time.

    Redacting at the logging boundary rather than at each call site is the
    same rule applied to expect_substring: one place to get right,
    and every sink downstream inherits it.
    """
    if not webhook:
        return text
    return text.replace(webhook, REDACTED)


class AlertKind(Enum):
    """
    What kind of state change produced this alert.

    Replaces the ``is_recovery``/``is_degraded`` boolean pair: two booleans
    encoded three real states plus one impossible combination (both True),
    resolved by a precedence rule that lived in a docstring. This concept
    already existed implicitly — the old code recomputed it as a string on
    every call — so naming it here formalises what was already there.

    The two CERT_EXPIRING members were appended later, with no
    signature change anywhere in the call chain — which was the point of
    naming this concept.

    Why two cert members instead of one plus a severity argument: every
    member maps 1:1 to a visual treatment in each channel's payload, and the
    project has decided repeatedly that colour carries triage (amber = worth
    your attention, red = urgent). A single member would force the severity
    through a second parameter that only one kind uses, and would break that
    1:1 mapping — the same mapping that lets an unhandled kind fail loudly
    instead of silently rendering as something it is not.
    """

    FAILURE            = "failure"
    RECOVERY           = "recovery"
    DEGRADED           = "degraded"
    CERT_EXPIRING_WARN = "cert_expiring_warn"
    CERT_EXPIRING_CRIT = "cert_expiring_crit"
    STILL_DOWN         = "still_down"


class Notifier(abc.ABC):
    """
    One alert channel, with the retry policy shared across all of them.

    Subclasses supply what genuinely differs per channel — the env var
    holding the webhook, the payload shape, and what HTTP status means
    success — and inherit the delivery policy. That policy is not generic
    boilerplate: it was designed early and later corrected (honour
    Discord's Retry-After instead of overriding it with our own backoff), so
    it lives in exactly one place where a fix reaches every channel at once.
    """

    #: Human-readable channel name, used in logs and metric labels.
    name: str = ""
    #: Environment variable holding this channel's webhook URL.
    env_var: str = ""
    #: HTTP status this channel returns on a successful post.
    success_status: int = 204

    @abc.abstractmethod
    def build_payload(
        self, url: str, status_detail: str, timestamp: str, kind: AlertKind
    ) -> dict:
        """
        Build this channel's JSON body. Pure: no I/O, no clock, no network.

        Kept separate from send() so the wire format of every channel is
        testable without touching the network — the same reason the original
        _build_discord_payload was a standalone function.
        """

    def webhook_url(self) -> str | None:
        """Return this channel's configured webhook, or None if unset."""
        return os.getenv(self.env_var) or None

    async def send(
        self,
        session      : aiohttp.ClientSession,
        url          : str,
        status_detail: str,
        kind         : AlertKind,
    ) -> bool:
        """
        Deliver one alert on this channel. Returns True only on confirmed
        delivery.

        Never raises. Every network error is caught and logged so a broken
        webhook can never mask the probe result that triggered it, and can
        never take down a sibling channel.

        The boolean return is what makes ``alerts_lost_all_channels_total``
        computable — "the alert was generated and nobody will hear about it"
        is a different event from "one channel failed", and only the caller
        seeing every channel's outcome can tell them apart.

        Retry policy
        ------------
        Timeouts, connection errors, HTTP 429 (rate-limited) and HTTP 5xx are
        retried up to WEBHOOK_RETRY_ATTEMPTS times with linear backoff. A
        non-retryable 4xx (malformed payload, revoked token) will not heal on
        retry: it is logged once and abandoned, since hammering a permanently
        broken webhook only delays the next probe cycle for no gain.
        """
        webhook = self.webhook_url()
        if webhook is None:
            # Not an error: an unconfigured channel is one the operator chose
            # not to use. dispatch_alert() is what notices if NO channel is
            # configured, which is the case that actually loses alerts.
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload   = self.build_payload(url, status_detail, timestamp, kind)

        for attempt in range(1, WEBHOOK_RETRY_ATTEMPTS + 1):
            is_last_attempt = attempt == WEBHOOK_RETRY_ATTEMPTS
            try:
                async with session.post(
                    webhook,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_S),
                ) as resp:
                    if resp.status == self.success_status:
                        logger.info(
                            "%s %s alert sent for %s. event_type=metric "
                            "metric=alerts_sent_total channel=%s value=1",
                            self.name, kind.value, url, self.name,
                        )
                        return True

                    retryable = resp.status == 429 or resp.status >= 500
                    if not retryable:
                        logger.error(
                            "%s webhook returned non-retryable HTTP %s for %s "
                            "alert on %s — abandoning (will not heal on retry). "
                            "event_type=metric metric=alerts_lost_total "
                            "channel=%s value=1 reason=non_retryable",
                            self.name, resp.status, kind.value, url, self.name,
                        )
                        return False
                    if is_last_attempt:
                        logger.error(
                            "%s %s alert LOST for %s: all %d attempts returned "
                            "HTTP %s. event_type=metric metric=alerts_lost_total "
                            "channel=%s value=1 reason=http_%s",
                            self.name, kind.value, url, WEBHOOK_RETRY_ATTEMPTS,
                            resp.status, self.name, resp.status,
                        )
                        return False

                    sleep_s = WEBHOOK_RETRY_BASE_S * attempt
                    if resp.status == 429:
                        # The channel tells us exactly how long to back off
                        # after a rate limit — ignoring it and retrying on our
                        # own linear schedule risks hitting the limit again
                        # immediately, burning attempts and losing the alert
                        # for no reason.
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            if retry_after is not None:
                                sleep_s = min(
                                    float(retry_after), WEBHOOK_RETRY_AFTER_CAP_S
                                )
                        except ValueError:
                            pass  # malformed header — fall back to linear backoff

                    logger.warning(
                        "%s webhook attempt %d/%d returned HTTP %s for %s alert "
                        "on %s — retrying in %.1fs",
                        self.name, attempt, WEBHOOK_RETRY_ATTEMPTS, resp.status,
                        kind.value, url, sleep_s,
                    )
                    await asyncio.sleep(sleep_s)

            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                if is_last_attempt:
                    logger.error(
                        "%s %s alert LOST for %s: all %d attempts raised %s: %s. "
                        "event_type=metric metric=alerts_lost_total channel=%s "
                        "value=1 reason=%s",
                        self.name, kind.value, url, WEBHOOK_RETRY_ATTEMPTS,
                        type(exc).__name__, _redact(str(exc), webhook),
                        self.name, type(exc).__name__,
                    )
                    return False

                sleep_s = WEBHOOK_RETRY_BASE_S * attempt
                logger.warning(
                    "%s webhook attempt %d/%d raised %s for %s alert on %s "
                    "— retrying in %.1fs",
                    self.name, attempt, WEBHOOK_RETRY_ATTEMPTS,
                    type(exc).__name__, kind.value, url, sleep_s,
                )
                await asyncio.sleep(sleep_s)

        return False  # unreachable: the loop always returns. Explicit for mypy.


class DiscordNotifier(Notifier):
    """Discord Webhook channel. Payload unchanged."""

    name           = "Discord"
    env_var        = "DISCORD_WEBHOOK_URL"
    success_status = 204  # Discord answers 204 No Content on success

    def build_payload(
        self, url: str, status_detail: str, timestamp: str, kind: AlertKind
    ) -> dict:
        """
        Colour-coded Discord embed.

        Green for recoveries, red for failures, amber for degradations. Amber
        is a distinct signal on purpose: a degraded service is not down, and
        paging it red trains the on-call to distrust red. The colour carries
        the triage.
        """
        if kind is AlertKind.RECOVERY:
            title        = "✅  Service Recovered"
            color        = 0x00C853  # green-A700
            status_label = "🟢  Current Status"
        elif kind is AlertKind.DEGRADED:
            title        = "⚠️  Service Degraded — Up But Hurting"
            color        = 0xFFB300  # amber-600
            status_label = "🟡  Degradation Detail"
        elif kind is AlertKind.CERT_EXPIRING_WARN:
            title        = "📅  Certificate Expiring — Renew Before It Bites"
            color        = 0xFFB300  # amber: worth your attention, not urgent
            status_label = "🟡  Certificate"
        elif kind is AlertKind.CERT_EXPIRING_CRIT:
            title        = "🚨  Certificate Expiring — Outage Has a Date"
            color        = 0xFF0000  # red: this becomes a full outage, soon
            status_label = "📛  Certificate"
        elif kind is AlertKind.STILL_DOWN:
            title        = "⏳  Still Down — This Has Been Going A While"
            color        = 0xFF0000  # same red as the first alert: same severity
            status_label = "🔴  Still DOWN"

        elif kind is AlertKind.FAILURE:
            title        = "🚨  Infrastructure Alert — Health Check Failed"
            color        = 0xFF0000  # red
            status_label = "📛  Failure Detail"
        else:
            # Not defensive padding: the previous shape ended in a bare else,
            # so a new AlertKind rendered silently as a red "Health Check
            # Failed" — a wrong statement about the service, in the channel
            # people trust. Raising instead surfaces it as the bug it is, and
            # dispatch_alert's gather already contains a raising notifier
            # without taking down the sibling channel.
            raise ValueError(f"unhandled AlertKind in Discord payload: {kind}")

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


class SlackNotifier(Notifier):
    """
    Slack Incoming Webhook channel.

    Slack has no embed colour on the message itself — the colour lives on an
    attachment wrapping the blocks, which is why the structure differs from
    Discord's rather than being the same dict with different keys. Forcing one
    shared builder would produce a worse message on both sides.

    ``text`` is set even though ``blocks`` carries the content: Slack uses it
    as the notification preview on mobile and desktop, and omitting it shows
    the user an empty push notification — which, for a channel that exists to
    catch the alert the other channel might miss, defeats the purpose.
    """

    name           = "Slack"
    env_var        = "SLACK_WEBHOOK_URL"
    success_status = 200  # Slack answers 200 with a plain "ok" body

    def build_payload(
        self, url: str, status_detail: str, timestamp: str, kind: AlertKind
    ) -> dict:
        if kind is AlertKind.RECOVERY:
            title        = "✅  Service Recovered"
            color        = "#00C853"  # same green as the Discord embed
            status_label = "Current Status"
        elif kind is AlertKind.DEGRADED:
            title        = "⚠️  Service Degraded — Up But Hurting"
            color        = "#FFB300"  # same amber
            status_label = "Degradation Detail"
        elif kind is AlertKind.CERT_EXPIRING_WARN:
            title        = "📅  Certificate Expiring — Renew Before It Bites"
            color        = "#FFB300"  # same amber as Discord: identical triage
            status_label = "Certificate"
        elif kind is AlertKind.CERT_EXPIRING_CRIT:
            title        = "🚨  Certificate Expiring — Outage Has a Date"
            color        = "#FF0000"  # same red as Discord
            status_label = "Certificate"
        elif kind is AlertKind.FAILURE:
            title        = "🚨  Infrastructure Alert — Health Check Failed"
            color        = "#FF0000"  # same red
            status_label = "Failure Detail"
        elif kind is AlertKind.STILL_DOWN:
            title        = "⏳  Still Down — This Has Been Going A While"
            color        = "#FF0000"   # same red as Discord: identical triage
            status_label = "Still DOWN"

        else:
            raise ValueError(f"unhandled AlertKind in Slack payload: {kind}")

        return {
            "text": f"{title} — {url}",  # mobile push preview
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {"type": "plain_text", "text": title, "emoji": True},
                        },
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Target URL*\n`{url}`"},
                                {
                                    "type": "mrkdwn",
                                    "text": f"*{status_label}*\n`{status_detail}`",
                                },
                                {"type": "mrkdwn", "text": f"*Timestamp (UTC)*\n`{timestamp}`"},
                            ],
                        },
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": "Async SRE Health Monitor • alerts on state-change only",
                                }
                            ],
                        },
                    ],
                }
            ],
        }


#: Every channel vigil-sre knows how to deliver on. A channel whose env var
#: is unset is skipped at dispatch time, so adding one here is inert until
#: the operator configures it.
ALL_NOTIFIERS: tuple[type[Notifier], ...] = (DiscordNotifier, SlackNotifier)


def active_notifiers() -> list[Notifier]:
    """
    Instantiate the channels that are actually configured.

    Presence of the environment variable is the switch — there is no separate
    config file to keep in sync with .env, and no way for a channel to be
    "enabled" while missing the webhook it needs.
    """
    candidates = [cls() for cls in ALL_NOTIFIERS]
    return [c for c in candidates if c.webhook_url() is not None]


async def dispatch_alert(
    session      : aiohttp.ClientSession,
    url          : str,
    status_detail: str,
    kind         : AlertKind,
    notifiers    : list[Notifier] | None = None,
) -> bool:
    """
    Deliver one alert on every configured channel, in parallel.

    Never raises — this sits on the alert-critical path and must not be able
    to fail a probe that already succeeded.

    Returns:
        True  — at least one channel accepted the alert; a human will see it.
        False — every channel failed, or none is configured. The caller MUST
                treat this as "nobody was told" and arrange a retry. Returning
                None here (as this did originally) meant the state machine
                recorded "already alerted" for an alert that was never
                delivered, and suppressed every following run — total silence
                with no way to notice it.

    Args:
        session:       Shared aiohttp.ClientSession for the run.
        url:           Target whose state just changed.
        status_detail: Human-readable reason, already redacted upstream (a
                       ${VAR} content assertion never reaches here in the
                       clear).
        kind:          Which state change this is.
        notifiers:     Override for tests, so a test never depends on the
                       ambient environment to decide which channels exist.
                       Defaults to whatever .env configures.
    """
    channels = active_notifiers() if notifiers is None else notifiers

    if not channels:
        logger.critical(
            "No alert channels configured (set %s) — %s alert for %s is LOST. "
            "event_type=metric metric=alerts_lost_all_channels_total value=1 "
            "reason=no_channels",
            " or ".join(cls.env_var for cls in ALL_NOTIFIERS), kind.value, url,
        )
        return False

    # The breaker lives HERE and not inside Notifier.send because it is a
    # dispatch policy, not a delivery detail: dispatch_alert is the only place
    # that sees every channel at once. It also means a Notifier subclass that
    # overrides send() inherits the breaker instead of quietly opting out of
    # it — the same structural-over-remembered rule this project applies to
    # escaping and redaction.
    async def _deliver(channel: Notifier) -> bool:
        if _circuit_is_open(channel.name):
            # Fail fast, but say so: the alert is still lost and still needs
            # to count as lost. What is skipped is the re-proving, never the
            # accounting.
            logger.warning(
                "%s circuit open after %d consecutive failures this run — "
                "skipping delivery for %s without retrying. event_type=metric "
                "metric=alerts_circuit_skipped_total channel=%s value=1",
                channel.name, CIRCUIT_BREAK_AFTER, url, channel.name,
            )
            return False
        return await channel.send(session, url, status_detail, kind)

    results = await asyncio.gather(
        *(_deliver(c) for c in channels),
        return_exceptions=True,
    )

    # An exception here means a notifier broke its own never-raise contract.
    # Log it as the defect it is rather than letting it read as a normal
    # delivery failure — and keep going, because the other channel's result
    # still matters.
    for channel, result in zip(channels, results):
        _record_circuit(channel.name, result is True)
        if isinstance(result, BaseException):
            logger.error(
                "%s notifier raised %s — this is a bug in that notifier, not a "
                "delivery failure: %r",
                channel.name, type(result).__name__, result,
            )

    delivered = any(result is True for result in results)
    if not delivered:
        # The only line here that means "an alert was generated and no human
        # will hear about it". Per-channel losses are diagnosis; this is the
        # one that should page.
        logger.critical(
            "%s alert for %s was LOST on ALL %d channel(s). event_type=metric "
            "metric=alerts_lost_all_channels_total value=1 reason=all_failed",
            kind.value.capitalize(), url, len(channels),
        )
    return delivered
