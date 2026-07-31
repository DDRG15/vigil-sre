"""
diagnostics.py — BDP / Latency-Bandwidth Diagnostics engine for vigil-sre.

What this module answers
------------------------
main.py answers "UP or DOWN?".  This module answers the next question:
"it is slow — WHY, and what would you tune?"

Every probe already opens a TCP connection, performs a TLS handshake, waits
for the server, and downloads a body.  Each of those phases carries a
distinct diagnostic signal:

    DNS lookup      → resolver health
    TCP connect     → round-trip time (physics: distance + routing)
    TLS handshake   → protocol overhead (TLS 1.3 vs 1.2, resumption)
    TTFB − RTT      → server-side processing time (backend, not network)
    body transfer   → goodput → effective TCP window → BDP estimate

Measurement honesty
-------------------
The bandwidth-delay product needs two inputs: RTT and bandwidth.  RTT is
sampled reliably with a dedicated raw TCP connect.  Bandwidth is NOT —
a health endpoint returning 5 KB finishes inside the TCP slow-start ramp
and tells you nothing about the path's capacity.  Therefore every
bandwidth-derived number in this module carries an explicit confidence
field: HIGH only when the response body was large enough
(≥ BANDWIDTH_MIN_SAMPLE_BYTES) to exercise the window; INSUFFICIENT_SAMPLE
otherwise, in which case no bandwidth rule is allowed to fire.  A diagnosis
based on a 5 KB sample is not a diagnosis — it is noise wearing a lab coat.

This module is pure computation plus one tiny network helper
(measure_tcp_rtt).  It performs no logging, no state writes, and no
alerting — main.py owns all side effects.

Python  : 3.11+
Depends : aiohttp (TraceConfig only), stdlib.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp

# ---------------------------------------------------------------------------
# Thresholds — module constants, same convention as main.py's constant block.
# Each one names the exact trigger of a diagnostic rule.
# ---------------------------------------------------------------------------
RTT_HIGH_MS               : float = 100.0    # above this, distance is the problem
DNS_SLOW_MS               : float = 200.0    # above this, the resolver is the problem
TTFB_BACKEND_SLACK_MS     : float = 500.0    # TTFB − RTT above this → backend slow
TLS_OVERHEAD_SLACK_MS     : float = 50.0     # tolerance on top of 2×RTT for TLS
BANDWIDTH_MIN_SAMPLE_BYTES: int   = 262_144  # 256 KB — below this, no BW verdicts
DEFAULT_WINDOW_BYTES      : int   = 65_536   # classic 64 KB window (no scaling)
WINDOW_LIMITED_FACTOR     : float = 0.9      # eff. window below 90% of 64 KB → limited
CERT_WARN_DAYS            : int   = 30
CERT_CRIT_DAYS            : int   = 7
RTT_PROBE_TIMEOUT_S       : float = 2.0      # raw TCP/TLS connect sample timeout
DEGRADED_TTFB_MS          : float = 1_500.0  # UP but slower than this → DEGRADED

CONFIDENCE_HIGH        : str = "HIGH"
CONFIDENCE_INSUFFICIENT: str = "INSUFFICIENT_SAMPLE"

SEVERITY_INFO    : str = "info"
SEVERITY_WARN    : str = "warn"
SEVERITY_CRITICAL: str = "critical"

# Diagnosis codes. Kept as constants so the DEGRADED policy and the tests
# reference the same strings the rules emit — a typo cannot silently unlink them.
CODE_HIGH_RTT         : str = "HIGH_RTT_NEEDS_EDGE"
CODE_SLOW_DNS         : str = "SLOW_DNS"
CODE_TLS_OVERHEAD     : str = "TLS_HANDSHAKE_OVERHEAD"
CODE_SLOW_BACKEND     : str = "SLOW_BACKEND"
CODE_WINDOW_LIMITED   : str = "WINDOW_LIMITED"
CODE_CERT_EXPIRING    : str = "CERT_EXPIRING"
CODE_HTTP2_UNSUPPORTED: str = "HTTP2_NOT_SUPPORTED"

# A service is DEGRADED when the probe succeeds but one of these performance
# findings fires. CERT_EXPIRING is excluded — an expiring cert is a scheduled
# future outage, not present degradation. HTTP2_NOT_SUPPORTED is excluded — it
# is an optimization hint, not a symptom the user is feeling right now.
DEGRADING_FINDING_CODES: frozenset[str] = frozenset({
    CODE_HIGH_RTT,
    CODE_SLOW_DNS,
    CODE_TLS_OVERHEAD,
    CODE_SLOW_BACKEND,
    CODE_WINDOW_LIMITED,
})


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class ProbePhases:
    """
    Per-phase timing breakdown of one successful probe.

    Every field that cannot always be measured is Optional and every
    consumer must tolerate None:
      * dns_ms            — None when the resolver cache hit or the target
                            is an IP literal (no lookup happened).
      * connect_total_ms  — None when the connection was reused from the pool.
      * rtt_ms            — None when the raw TCP sample failed (firewall,
                            transient refusal).  Diagnostics degrade, the
                            probe itself never fails because of this.
      * tls_ms            — derived (connect_total − rtt); only meaningful on
                            a fresh HTTPS connection with a valid RTT sample.
      * goodput_bps       — None when the transfer completed faster than the
                            clock resolution (division by ~zero is a lie).
      * tls_cert_days_left— None for plain HTTP or when the transport does
                            not expose the peer certificate.
    """

    url                 : str
    http_status         : int
    ttfb_ms             : float
    transfer_ms         : float
    body_bytes          : int
    connection_reused   : bool  = False
    dns_ms              : float | None = None
    connect_total_ms    : float | None = None
    rtt_ms              : float | None = None
    tls_ms              : float | None = None
    server_processing_ms: float | None = None
    goodput_bps         : float | None = None
    tls_cert_days_left  : int   | None = None
    alpn_protocol       : str   | None = None   # e.g. "h2", "http/1.1"; None if unknown
    h2_supported        : bool  | None = None   # server offered h2 in ALPN; None if untested


@dataclass
class Diagnosis:
    """One actionable finding produced by analyze()."""

    code          : str   # stable identifier, e.g. "WINDOW_LIMITED"
    severity      : str   # SEVERITY_INFO | SEVERITY_WARN | SEVERITY_CRITICAL
    evidence      : str   # the measured numbers that triggered the rule
    recommendation: str   # what to change — and what tuning will NOT fix


# ---------------------------------------------------------------------------
# Connection sampling — raw TCP RTT + a controlled TLS handshake
# ---------------------------------------------------------------------------

@dataclass
class ConnectionSample:
    """
    What one out-of-band connection sample measured, before the HTTP probe.

    Every field is Optional and every consumer tolerates None:
      * rtt_ms        — None when the TCP connect failed (firewall, refusal).
      * tls_ms        — None for plain HTTP, or when the TLS sample failed /
                        the subtraction went negative (measurement noise).
      * alpn_protocol — the protocol the server negotiated when offered both
                        h2 and http/1.1; None when not sampled or unsupported.
      * h2_supported  — True/False derived from alpn_protocol; None if untested.
    """

    rtt_ms       : float | None = None
    tls_ms       : float | None = None
    alpn_protocol: str   | None = None
    h2_supported : bool  | None = None


def host_port_from_url(url: str) -> tuple[str, int] | None:
    """
    Extract (hostname, port) from *url* for the connection sample.

    Returns None for URLs without a hostname — the caller skips the sample
    and diagnostics degrade gracefully.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname, port


async def _timed_tcp_connect(
    host: str, port: int, timeout_s: float
) -> float | None:
    """One raw TCP connect (SYN → SYN-ACK), timed and closed. ms or None."""
    start = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_s
        )
    except (OSError, asyncio.TimeoutError):
        return None

    rtt_ms = (time.monotonic() - start) * 1000.0
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass  # the sample is already taken; a noisy close changes nothing
    return rtt_ms


def _permissive_tls_context() -> ssl.SSLContext:
    """
    A TLS context that completes the handshake even against an invalid or
    self-signed certificate, and offers both h2 and http/1.1 in ALPN.

    Verification is disabled on purpose: this context times the handshake and
    reads the negotiated ALPN — it never trusts the peer and never carries
    application data. Certificate expiry is read from the *validated* aiohttp
    probe connection, not here, so disabling verification here loses nothing.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_alpn_protocols(["h2", "http/1.1"])
    except NotImplementedError:
        pass  # ancient OpenSSL without ALPN — alpn stays None, rule stays quiet
    return ctx


async def sample_connection(
    host: str,
    port: int,
    use_tls: bool,
    server_hostname: str | None = None,
    timeout_s: float = RTT_PROBE_TIMEOUT_S,
) -> ConnectionSample:
    """
    Sample the path out-of-band: one plain TCP connect for RTT, then (for TLS
    targets) one handshake that offers h2/http1.1 to time the TLS phase and
    learn whether the server speaks HTTP/2.

    Why sample instead of reusing aiohttp's timings?
    ------------------------------------------------
    aiohttp's on_connection_create_* hooks fuse TCP connect and TLS into one
    opaque interval, so they can give neither a clean RTT nor a clean TLS time.
    And aiohttp only ever offers http/1.1, so its negotiated ALPN can never
    reveal server h2 support. This sampler answers both questions on
    connections it controls, at the cost of one (HTTP) or two (HTTPS) extra
    handshakes per target per run — a SYN/FIN pair, negligible.

    Returns:
        A ConnectionSample. Any field may be None; nothing here ever raises
        into the caller, because a diagnostics sample must never fail a probe.
    """
    rtt_ms = await _timed_tcp_connect(host, port, timeout_s)

    if not use_tls:
        return ConnectionSample(rtt_ms=rtt_ms)

    ctx = _permissive_tls_context()
    tls_start = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, port, ssl=ctx, server_hostname=server_hostname or host
            ),
            timeout=timeout_s,
        )
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return ConnectionSample(rtt_ms=rtt_ms)

    tls_total_ms = (time.monotonic() - tls_start) * 1000.0
    ssl_object = writer.get_extra_info("ssl_object")
    alpn = ssl_object.selected_alpn_protocol() if ssl_object is not None else None

    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass

    # TLS handshake time is the fresh-connect total minus the bare round trip,
    # both measured back-to-back by this sampler. Clamp negatives (noise) to
    # None. Unlike deriving from aiohttp's warm connect, both figures come from
    # the same sampler moments apart, so disagreement is rare.
    tls_ms: float | None = None
    if rtt_ms is not None:
        derived = tls_total_ms - rtt_ms
        tls_ms = derived if derived > 0 else None

    h2_supported = None if alpn is None else (alpn == "h2")
    return ConnectionSample(
        rtt_ms=rtt_ms, tls_ms=tls_ms, alpn_protocol=alpn, h2_supported=h2_supported
    )


# ---------------------------------------------------------------------------
# TLS certificate expiry
# ---------------------------------------------------------------------------

def cert_days_left(peercert: dict | None) -> int | None:
    """
    Days until the peer certificate expires, from getpeercert()'s dict form.

    Returns None when the cert or its notAfter field is unavailable.
    Negative values mean the certificate is already expired.
    """
    if not peercert:
        return None
    not_after = peercert.get("notAfter")
    if not not_after:
        return None
    try:
        expiry_ts = ssl.cert_time_to_seconds(not_after)
    except ValueError:
        return None
    remaining_s = expiry_ts - datetime.now(timezone.utc).timestamp()
    return int(remaining_s // 86_400)


# ---------------------------------------------------------------------------
# aiohttp TraceConfig — DNS + connection-create timings per request
# ---------------------------------------------------------------------------

def build_trace_config() -> aiohttp.TraceConfig:
    """
    Build a TraceConfig whose hooks write phase timings into the dict passed
    as ``trace_request_ctx`` on each request.

    Keys written:
        dns_ms              — resolver wall time (absent on cache hit)
        connect_total_ms    — TCP connect + TLS handshake (fresh conn only)
        connection_reused   — True when the pool served an existing conn
    """
    trace = aiohttp.TraceConfig()

    async def on_dns_start(
        session: aiohttp.ClientSession,
        ctx: SimpleNamespace,
        params: aiohttp.TraceDnsResolveHostStartParams,
    ) -> None:
        if isinstance(ctx.trace_request_ctx, dict):
            ctx.trace_request_ctx["_dns_start"] = time.monotonic()

    async def on_dns_end(
        session: aiohttp.ClientSession,
        ctx: SimpleNamespace,
        params: aiohttp.TraceDnsResolveHostEndParams,
    ) -> None:
        store = ctx.trace_request_ctx
        if isinstance(store, dict) and "_dns_start" in store:
            store["dns_ms"] = (time.monotonic() - store.pop("_dns_start")) * 1000.0

    async def on_conn_start(
        session: aiohttp.ClientSession,
        ctx: SimpleNamespace,
        params: aiohttp.TraceConnectionCreateStartParams,
    ) -> None:
        if isinstance(ctx.trace_request_ctx, dict):
            ctx.trace_request_ctx["_conn_start"] = time.monotonic()

    async def on_conn_end(
        session: aiohttp.ClientSession,
        ctx: SimpleNamespace,
        params: aiohttp.TraceConnectionCreateEndParams,
    ) -> None:
        store = ctx.trace_request_ctx
        if isinstance(store, dict) and "_conn_start" in store:
            store["connect_total_ms"] = (
                time.monotonic() - store.pop("_conn_start")
            ) * 1000.0

    async def on_conn_reuse(
        session: aiohttp.ClientSession,
        ctx: SimpleNamespace,
        params: aiohttp.TraceConnectionReuseconnParams,
    ) -> None:
        if isinstance(ctx.trace_request_ctx, dict):
            ctx.trace_request_ctx["connection_reused"] = True

    trace.on_dns_resolvehost_start.append(on_dns_start)
    trace.on_dns_resolvehost_end.append(on_dns_end)
    trace.on_connection_create_start.append(on_conn_start)
    trace.on_connection_create_end.append(on_conn_end)
    trace.on_connection_reuseconn.append(on_conn_reuse)
    return trace


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def bandwidth_confidence(phases: ProbePhases) -> str:
    """HIGH only when the body was large enough to exercise the TCP window."""
    if phases.body_bytes >= BANDWIDTH_MIN_SAMPLE_BYTES:
        return CONFIDENCE_HIGH
    return CONFIDENCE_INSUFFICIENT


def effective_window_bytes(phases: ProbePhases) -> float | None:
    """
    Bytes the connection effectively kept in flight: goodput × RTT.

    This is the observable floor of the path's BDP.  If it sits below the
    classic 64 KB window on a large transfer, the transfer was window-limited
    — the pipe was never full.
    """
    if phases.goodput_bps is None or phases.rtt_ms is None:
        return None
    goodput_bytes_per_s = phases.goodput_bps / 8.0
    return goodput_bytes_per_s * (phases.rtt_ms / 1000.0)


def default_window_ceiling_bps(phases: ProbePhases) -> float | None:
    """
    Throughput ceiling a plain 64 KB window imposes on this path:
    window / RTT.  On a 150 ms path that is ~3.5 Mbit/s no matter how fat
    the pipe is — which is exactly why window scaling exists.
    """
    if phases.rtt_ms is None or phases.rtt_ms <= 0:
        return None
    return DEFAULT_WINDOW_BYTES * 8.0 / (phases.rtt_ms / 1000.0)


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

def analyze(phases: ProbePhases, *, rtt_high_ms: float = RTT_HIGH_MS) -> list[Diagnosis]:
    """
    Run every diagnostic rule against *phases* and return the findings.

    Rule discipline: each rule names its trigger (a threshold constant) and
    tolerates missing inputs by not firing.  Bandwidth rules additionally
    require CONFIDENCE_HIGH — no verdicts from insufficient samples.

    Args:
        phases:      The measured probe phases.
        rtt_high_ms: Per-target override of RTT_HIGH_MS (rules 1 and 7 — both
                     concern the same physical quantity, so both honour the
                     same override). A single global threshold produces
                     chronic false HIGH_RTT_NEEDS_EDGE findings for a target
                     that is legitimately far away; a chronically-false
                     finding gets ignored, which defeats the point of having it.
    """
    findings: list[Diagnosis] = []

    # --- Rule 1: high RTT — distance/routing, not tunable ------------------
    if phases.rtt_ms is not None and phases.rtt_ms > rtt_high_ms:
        findings.append(Diagnosis(
            code=CODE_HIGH_RTT,
            severity=SEVERITY_WARN,
            evidence=f"rtt_ms={phases.rtt_ms:.0f} (threshold {rtt_high_ms:.0f})",
            recommendation=(
                "RTT this high is distance or routing — no sysctl shrinks the "
                "speed of light. Serve this endpoint from a CDN or an edge/"
                "anycast region closer to the probe. TCP tuning only helps "
                "throughput on this path, never latency."
            ),
        ))

    # --- Rule 2: slow DNS ---------------------------------------------------
    if phases.dns_ms is not None and phases.dns_ms > DNS_SLOW_MS:
        findings.append(Diagnosis(
            code=CODE_SLOW_DNS,
            severity=SEVERITY_WARN,
            evidence=f"dns_ms={phases.dns_ms:.0f} (threshold {DNS_SLOW_MS:.0f})",
            recommendation=(
                "The resolver, not the target, spent this time. Run a local "
                "caching resolver (systemd-resolved, dnsmasq, unbound) or "
                "point at a faster upstream (1.1.1.1 / 8.8.8.8), and check "
                "the record's TTL — a 30 s TTL forces constant re-resolution."
            ),
        ))

    # --- Rule 3: TLS handshake overhead ------------------------------------
    if (
        phases.tls_ms is not None
        and phases.rtt_ms is not None
        and phases.tls_ms > 2 * phases.rtt_ms + TLS_OVERHEAD_SLACK_MS
    ):
        findings.append(Diagnosis(
            code=CODE_TLS_OVERHEAD,
            severity=SEVERITY_WARN,
            evidence=(
                f"tls_ms={phases.tls_ms:.0f} vs 2xRTT+{TLS_OVERHEAD_SLACK_MS:.0f}"
                f"={2 * phases.rtt_ms + TLS_OVERHEAD_SLACK_MS:.0f}"
            ),
            recommendation=(
                "The handshake costs more round trips than TLS 1.3 needs "
                "(1-RTT, 0-RTT on resumption). Enable TLS 1.3 on the server, "
                "turn on session resumption/tickets, and staple OCSP so the "
                "client skips the revocation round trip."
            ),
        ))

    # --- Rule 4: slow backend (network exonerated) --------------------------
    if (
        phases.server_processing_ms is not None
        and phases.server_processing_ms > TTFB_BACKEND_SLACK_MS
    ):
        findings.append(Diagnosis(
            code=CODE_SLOW_BACKEND,
            severity=SEVERITY_WARN,
            evidence=(
                f"server_processing_ms={phases.server_processing_ms:.0f} "
                f"(TTFB minus RTT, threshold {TTFB_BACKEND_SLACK_MS:.0f})"
            ),
            recommendation=(
                "The network already delivered the request — the server sat "
                "on it. This is application time: profile the handler, the "
                "database queries behind it, or cold starts. A CDN or TCP "
                "tuning will not move this number."
            ),
        ))

    # --- Rule 5: window-limited transfer (bandwidth confidence gated) -------
    if bandwidth_confidence(phases) == CONFIDENCE_HIGH:
        eff_window = effective_window_bytes(phases)
        ceiling = default_window_ceiling_bps(phases)
        if (
            eff_window is not None
            and eff_window < DEFAULT_WINDOW_BYTES * WINDOW_LIMITED_FACTOR
        ):
            ceiling_txt = (
                f"; a 64KB window caps this path at {ceiling / 1e6:.1f} Mbit/s"
                if ceiling is not None else ""
            )
            findings.append(Diagnosis(
                code=CODE_WINDOW_LIMITED,
                severity=SEVERITY_WARN,
                evidence=(
                    f"effective_window={eff_window:.0f}B over a "
                    f"{phases.body_bytes}B transfer at rtt_ms="
                    f"{phases.rtt_ms:.0f}{ceiling_txt}"
                ),
                recommendation=(
                    "The transfer never filled even a classic 64KB window — "
                    "throughput is window-limited, not pipe-limited. Verify "
                    "net.ipv4.tcp_window_scaling=1 on both ends and raise "
                    "net.core.rmem_max / net.ipv4.tcp_rmem on the receiver. "
                    "If the server is remote and untunable, a CDN edge "
                    "shortens the RTT, which shrinks the BDP the window must "
                    "cover."
                ),
            ))

    # --- Rule 6: certificate expiry -----------------------------------------
    if phases.tls_cert_days_left is not None:
        if phases.tls_cert_days_left < CERT_CRIT_DAYS:
            findings.append(Diagnosis(
                code=CODE_CERT_EXPIRING,
                severity=SEVERITY_CRITICAL,
                evidence=(
                    f"tls_cert_days_left={phases.tls_cert_days_left} "
                    f"(critical below {CERT_CRIT_DAYS})"
                ),
                recommendation=(
                    "Renew the certificate NOW. When it expires every client "
                    "hard-fails the handshake — this outage has a scheduled "
                    "date and it is on the certificate."
                ),
            ))
        elif phases.tls_cert_days_left < CERT_WARN_DAYS:
            findings.append(Diagnosis(
                code=CODE_CERT_EXPIRING,
                severity=SEVERITY_WARN,
                evidence=(
                    f"tls_cert_days_left={phases.tls_cert_days_left} "
                    f"(warn below {CERT_WARN_DAYS})"
                ),
                recommendation=(
                    "Renew the certificate before the window closes, or wire "
                    "up auto-renewal (certbot/ACME) so this warning never "
                    "fires again."
                ),
            ))

    # --- Rule 7: server does not offer HTTP/2 on a high-latency path --------
    # Fires only when the server was actually tested (h2_supported is not None)
    # and explicitly declined h2, AND the path is slow enough that HTTP/1.1
    # head-of-line blocking bites. On a fast LAN this is noise, so it stays
    # quiet there.
    if (
        phases.h2_supported is False
        and phases.rtt_ms is not None
        and phases.rtt_ms > rtt_high_ms
    ):
        findings.append(Diagnosis(
            code=CODE_HTTP2_UNSUPPORTED,
            severity=SEVERITY_INFO,
            evidence=(
                f"alpn={phases.alpn_protocol!r} h2=no at rtt_ms="
                f"{phases.rtt_ms:.0f} (threshold {rtt_high_ms:.0f})"
            ),
            recommendation=(
                "The server only offers HTTP/1.1. On this high-RTT path, "
                "HTTP/1.1 head-of-line blocking serialises requests that "
                "HTTP/2 would multiplex over one connection. Enable HTTP/2 "
                "(h2) on the server or its load balancer/CDN edge — the win "
                "grows with RTT and with the number of resources per page."
            ),
        ))

    return findings


def cert_alert_threshold(
    days_left: int | None, already_alerted_at: int | None
) -> int | None:
    """
    Decide whether an expiring certificate warrants an alert right now, and
    at which threshold — or None for silence (the design note, an earlier release).

    Pure by design: the whole suppression rule lives here, so the table of
    cases below is testable with plain calls — no probe, no state file, no
    webhook. That matters because the risk in this feature is not "does it
    alert" but "does it stop alerting": a certificate sits below its warning
    threshold for 30 days, which is 43,200 runs, and 43,198 of them must stay
    quiet.

    The stored value is the THRESHOLD last alerted at, not the days
    remaining. Days remaining change every single day and are useless for
    "have I already said this"; the threshold is stable for the whole band.

        days  stored  ->  result
          25       —      alert at 30 (first warn crossing)
          24      30      silence
           6      30      alert at 7 (escalation to critical)
           5       7      silence
          90       7      clear — renewed, so the next expiry alerts again
          25       —      alert at 30 (a year later, as if new)

    The reset on renewal is the case that cannot be dropped. Without it a
    renewed certificate keeps ``stored = 7`` forever, and its next expiry
    would skip the 30-day warning entirely — the monitor going quiet exactly
    at the notice that gives the most room to act.

    Args:
        days_left:          phases.tls_cert_days_left; None for plain HTTP or
                            when the transport did not expose a certificate.
        already_alerted_at: The threshold this target was last alerted at, or
                            None if never (or since renewed).

    Returns:
        CERT_CRIT_DAYS or CERT_WARN_DAYS when an alert should fire now, else
        None. A None return with a healthy certificate is also the caller's
        signal to clear any stored threshold — see is_cert_healthy().
    """
    if days_left is None:
        return None

    if days_left < CERT_CRIT_DAYS:
        threshold = CERT_CRIT_DAYS
    elif days_left < CERT_WARN_DAYS:
        threshold = CERT_WARN_DAYS
    else:
        return None  # healthy — nothing to say

    if already_alerted_at is None or threshold < already_alerted_at:
        return threshold

    # Known limitation (the design note): a partial renewal landing back inside the
    # 7-30 day band does not re-alert, because 30 < 7 is false. Defensible —
    # the operator already got the critical — and rare enough not to
    # complicate the rule. Documented, not forgotten.
    return None


def is_cert_healthy(days_left: int | None) -> bool:
    """
    True when a certificate is far enough out that any stored alert threshold
    should be cleared — the renewal case in cert_alert_threshold's table.

    Separate from cert_alert_threshold because "should I alert" and "should I
    forget that I alerted" are different questions, and collapsing them into
    one return value is how the reset gets lost in a refactor.
    """
    return days_left is not None and days_left >= CERT_WARN_DAYS


def degraded_reason(
    phases: ProbePhases,
    findings: list[Diagnosis],
    *,
    degraded_ttfb_ms: float = DEGRADED_TTFB_MS,
) -> str | None:
    """
    Decide whether a *successful* probe should count as DEGRADED, and why.

    A 200 is not the same as healthy. A service answering 200 in 2.8 seconds,
    or tripping a performance finding, is up and hurting — the state machine
    must be able to say so without waiting for it to go fully DOWN.

    Args:
        phases:           The measured probe phases.
        findings:         The findings analyze() already produced for phases.
        degraded_ttfb_ms: Per-target override of DEGRADED_TTFB_MS. A report
                          that legitimately takes 3s is a chronic false
                          DEGRADED under the global default — the whole point
                          of a per-target SLA override.

    Returns:
        A short human-readable reason string when the service is degraded, or
        None when it is cleanly healthy. The reason is what lands in the
        yellow alert and in state.json's last_error field.
    """
    degrading = [f for f in findings if f.code in DEGRADING_FINDING_CODES]
    if degrading:
        return "; ".join(f"{f.code} ({f.evidence})" for f in degrading)

    if phases.ttfb_ms > degraded_ttfb_ms:
        return f"SLOW_RESPONSE (ttfb_ms={phases.ttfb_ms:.0f} > {degraded_ttfb_ms:.0f})"

    return None


# ---------------------------------------------------------------------------
# Serialization for state.json
# ---------------------------------------------------------------------------

def phases_to_dict(phases: ProbePhases, findings: list[Diagnosis]) -> dict:
    """
    Serialize one measurement + its findings for persistence in state.json.

    Numbers are rounded to keep the file human-readable; None survives as
    null so a consumer can tell "not measured" from zero.
    """
    def _r(value: float | None, digits: int = 1) -> float | None:
        return None if value is None else round(value, digits)

    eff_window = effective_window_bytes(phases)
    ceiling = default_window_ceiling_bps(phases)

    return {
        "measured": {
            "http_status"         : phases.http_status,
            "rtt_ms"              : _r(phases.rtt_ms),
            "dns_ms"              : _r(phases.dns_ms),
            "connect_total_ms"    : _r(phases.connect_total_ms),
            "tls_ms"              : _r(phases.tls_ms),
            "ttfb_ms"             : _r(phases.ttfb_ms),
            "server_processing_ms": _r(phases.server_processing_ms),
            "transfer_ms"         : _r(phases.transfer_ms),
            "body_bytes"          : phases.body_bytes,
            "goodput_bps"         : _r(phases.goodput_bps, 0),
            "connection_reused"   : phases.connection_reused,
            "tls_cert_days_left"  : phases.tls_cert_days_left,
            "alpn_protocol"       : phases.alpn_protocol,
            "h2_supported"        : phases.h2_supported,
        },
        "derived": {
            "effective_window_bytes"    : _r(eff_window, 0),
            "default_window_ceiling_bps": _r(ceiling, 0),
            "bandwidth_confidence"      : bandwidth_confidence(phases),
        },
        "findings": [
            {
                "code"          : d.code,
                "severity"      : d.severity,
                "evidence"      : d.evidence,
                "recommendation": d.recommendation,
            }
            for d in findings
        ],
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
