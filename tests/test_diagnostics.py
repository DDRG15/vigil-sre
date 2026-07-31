"""
tests/test_diagnostics.py — Test suite for the BDP / latency diagnostics engine.

Coverage:
  A. host_port_from_url()   —  parsing + default ports
  B. cert_days_left()       —  expiry math + missing/garbage inputs
  C. analyze() rules        —  each rule: one firing case, one just-below case
  D. confidence gating      —  bandwidth rules stay silent on small samples
  E. measure_tcp_rtt()      —  real local server (hit) + closed port (None)
  F. phases_to_dict()       —  serialization shape + None survival
  G. cert_alert_threshold() —  the suppression rule, case by case

Run: pytest tests/ -v

Discipline note
---------------
Every rule is tested at BOTH sides of its threshold. A rule that fires is
only half a test — a rule that fails to stay quiet just below its trigger is
how false alarms are born, and a monitoring tool that cries wolf gets muted,
and a muted monitor is worse than none.
"""

from __future__ import annotations

import asyncio
import ssl
import sys
import os
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics import (
    BANDWIDTH_MIN_SAMPLE_BYTES,
    CERT_CRIT_DAYS,
    CERT_WARN_DAYS,
    CODE_CERT_EXPIRING,
    CODE_HIGH_RTT,
    CODE_HTTP2_UNSUPPORTED,
    CODE_SLOW_BACKEND,
    CODE_SLOW_DNS,
    CODE_TLS_OVERHEAD,
    CODE_WINDOW_LIMITED,
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    DEGRADED_TTFB_MS,
    DEGRADING_FINDING_CODES,
    ConnectionSample,
    Diagnosis,
    ProbePhases,
    analyze,
    bandwidth_confidence,
    cert_alert_threshold,
    cert_days_left,
    default_window_ceiling_bps,
    degraded_reason,
    is_cert_healthy,
    is_cert_renewed,
    effective_window_bytes,
    host_port_from_url,
    phases_to_dict,
    sample_connection,
)

# Every diagnosis code the rule engine can emit. Kept as its own set (not
# derived from the module) so this list is a second, independent witness —
# if a rule and this list ever disagree, that disagreement is the point.
ALL_DIAGNOSIS_CODES: frozenset[str] = frozenset({
    CODE_HIGH_RTT,
    CODE_SLOW_DNS,
    CODE_TLS_OVERHEAD,
    CODE_SLOW_BACKEND,
    CODE_WINDOW_LIMITED,
    CODE_CERT_EXPIRING,
    CODE_HTTP2_UNSUPPORTED,
})


def _codes(findings: list[Diagnosis]) -> set[str]:
    return {f.code for f in findings}


def _base_phases(**overrides) -> ProbePhases:
    """A neutral, all-healthy ProbePhases; override single fields per test."""
    defaults = dict(
        url="https://example.com",
        http_status=200,
        ttfb_ms=40.0,
        transfer_ms=5.0,
        body_bytes=1024,
        connection_reused=False,
        dns_ms=10.0,
        connect_total_ms=30.0,
        rtt_ms=20.0,
        tls_ms=10.0,
        server_processing_ms=10.0,
        goodput_bps=None,
        tls_cert_days_left=200,
        alpn_protocol="h2",
        h2_supported=True,
    )
    defaults.update(overrides)
    return ProbePhases(**defaults)


# =============================================================================
# A. host_port_from_url()
# =============================================================================


def test_host_port_https_default() -> None:
    assert host_port_from_url("https://example.com/health") == ("example.com", 443)


def test_host_port_http_default() -> None:
    assert host_port_from_url("http://example.com") == ("example.com", 80)


def test_host_port_explicit() -> None:
    assert host_port_from_url("https://example.com:8443/x") == ("example.com", 8443)


def test_host_port_no_hostname() -> None:
    assert host_port_from_url("not-a-url") is None


# =============================================================================
# B. cert_days_left()
# =============================================================================


def test_cert_days_left_future() -> None:
    future = datetime.now(timezone.utc) + timedelta(days=45)
    not_after = future.strftime("%b %d %H:%M:%S %Y GMT")
    days = cert_days_left({"notAfter": not_after})
    assert 43 <= days <= 45


def test_cert_days_left_expired() -> None:
    past = datetime.now(timezone.utc) - timedelta(days=5)
    not_after = past.strftime("%b %d %H:%M:%S %Y GMT")
    assert cert_days_left({"notAfter": not_after}) < 0


def test_cert_days_left_none() -> None:
    assert cert_days_left(None) is None


def test_cert_days_left_missing_field() -> None:
    assert cert_days_left({"subject": "x"}) is None


def test_cert_days_left_garbage() -> None:
    assert cert_days_left({"notAfter": "not a date"}) is None


# =============================================================================
# C. analyze() — one firing + one just-below case per rule
# =============================================================================


def test_high_rtt_fires() -> None:
    findings = analyze(_base_phases(rtt_ms=150.0, connect_total_ms=160.0, tls_ms=10.0))
    assert CODE_HIGH_RTT in _codes(findings)


def test_high_rtt_silent_below_threshold() -> None:
    findings = analyze(_base_phases(rtt_ms=80.0))
    assert CODE_HIGH_RTT not in _codes(findings)


def test_slow_dns_fires() -> None:
    findings = analyze(_base_phases(dns_ms=300.0))
    assert CODE_SLOW_DNS in _codes(findings)


def test_slow_dns_silent_below_threshold() -> None:
    findings = analyze(_base_phases(dns_ms=150.0))
    assert CODE_SLOW_DNS not in _codes(findings)


def test_tls_overhead_fires() -> None:
    # rtt=20 → 2*rtt+50 = 90; tls of 120 exceeds it
    findings = analyze(_base_phases(rtt_ms=20.0, tls_ms=120.0, connect_total_ms=140.0))
    assert CODE_TLS_OVERHEAD in _codes(findings)


def test_tls_overhead_silent_when_reasonable() -> None:
    # rtt=20 → threshold 90; tls of 40 is fine
    findings = analyze(_base_phases(rtt_ms=20.0, tls_ms=40.0, connect_total_ms=60.0))
    assert CODE_TLS_OVERHEAD not in _codes(findings)


def test_slow_backend_fires() -> None:
    findings = analyze(_base_phases(server_processing_ms=800.0))
    assert CODE_SLOW_BACKEND in _codes(findings)


def test_slow_backend_silent_below_threshold() -> None:
    findings = analyze(_base_phases(server_processing_ms=200.0))
    assert CODE_SLOW_BACKEND not in _codes(findings)


def test_cert_warn_fires() -> None:
    findings = analyze(_base_phases(tls_cert_days_left=20))
    cert = [f for f in findings if f.code == CODE_CERT_EXPIRING]
    assert len(cert) == 1
    assert cert[0].severity == "warn"


def test_cert_critical_fires() -> None:
    findings = analyze(_base_phases(tls_cert_days_left=3))
    cert = [f for f in findings if f.code == CODE_CERT_EXPIRING]
    assert len(cert) == 1
    assert cert[0].severity == "critical"


def test_cert_healthy_silent() -> None:
    findings = analyze(_base_phases(tls_cert_days_left=200))
    assert CODE_CERT_EXPIRING not in _codes(findings)


def test_missing_rtt_suppresses_rtt_dependent_rules() -> None:
    """A None RTT must not crash analyze() and must skip RTT-based rules."""
    findings = analyze(_base_phases(rtt_ms=None, tls_ms=None, server_processing_ms=None))
    assert CODE_HIGH_RTT not in _codes(findings)
    assert CODE_TLS_OVERHEAD not in _codes(findings)


# =============================================================================
# C2. Anti-drift invariant (audit fix: rules 1-6 used to emit string literals
# instead of the CODE_* constants, so a future rename of a constant could
# silently unlink it from the rule that is supposed to emit it, and from
# DEGRADING_FINDING_CODES, without any test going red).
# =============================================================================


def test_degrading_codes_are_a_subset_of_all_known_codes() -> None:
    """DEGRADING_FINDING_CODES must only ever reference codes analyze() can
    actually emit — a typo or a stale entry here would silently misclassify
    DEGRADED status without any rule ever producing that exact string."""
    assert DEGRADING_FINDING_CODES <= ALL_DIAGNOSIS_CODES


def test_every_rule_emits_its_named_constant_not_a_stray_literal() -> None:
    """Fire every rule at once and assert the emitted codes are drawn from
    ALL_DIAGNOSIS_CODES. This is the regression guard for the audit finding:
    if a rule ever reverts to (or drifts to) a literal that doesn't match its
    CODE_* constant, the emitted code stops belonging to this known set and
    this test goes red — where asserting against a hand-typed string literal,
    the same class of bug the original code had, would not have caught it."""
    findings = analyze(_base_phases(
        rtt_ms=150.0, connect_total_ms=170.0, tls_ms=130.0,
        dns_ms=300.0, server_processing_ms=800.0,
        tls_cert_days_left=3, h2_supported=False, alpn_protocol="http/1.1",
    ))
    emitted = _codes(findings)
    assert emitted  # sanity: this mix must fire something
    assert emitted <= ALL_DIAGNOSIS_CODES


# =============================================================================
# D. Bandwidth confidence gating
# =============================================================================


def test_window_limited_fires_on_large_sample() -> None:
    """Large body + goodput implying a tiny effective window → WINDOW_LIMITED."""
    body = BANDWIDTH_MIN_SAMPLE_BYTES + 1
    # goodput 1 Mbit/s, rtt 100ms → eff window = (1e6/8)*0.1 = 12500 B << 64KB
    findings = analyze(_base_phases(
        body_bytes=body, goodput_bps=1_000_000.0, rtt_ms=100.0,
        connect_total_ms=110.0, tls_ms=10.0,
    ))
    assert CODE_WINDOW_LIMITED in _codes(findings)


def test_window_limited_silent_on_small_sample() -> None:
    """Same throughput shape but a small body → confidence gate blocks it."""
    findings = analyze(_base_phases(
        body_bytes=1024, goodput_bps=1_000_000.0, rtt_ms=100.0,
        connect_total_ms=110.0, tls_ms=10.0,
    ))
    assert CODE_WINDOW_LIMITED not in _codes(findings)


def test_confidence_high_only_on_large_body() -> None:
    assert bandwidth_confidence(_base_phases(body_bytes=BANDWIDTH_MIN_SAMPLE_BYTES)) == CONFIDENCE_HIGH
    assert bandwidth_confidence(_base_phases(body_bytes=1024)) == CONFIDENCE_INSUFFICIENT


def test_effective_window_none_without_goodput() -> None:
    assert effective_window_bytes(_base_phases(goodput_bps=None)) is None


def test_default_window_ceiling_math() -> None:
    # 64KB window over 100ms RTT → 65536*8/0.1 = 5,242,880 bps
    ceiling = default_window_ceiling_bps(_base_phases(rtt_ms=100.0))
    assert ceiling == pytest.approx(5_242_880.0)


def test_default_window_ceiling_none_without_rtt() -> None:
    assert default_window_ceiling_bps(_base_phases(rtt_ms=None)) is None


# =============================================================================
# E. sample_connection()
# =============================================================================


async def test_sample_connection_plain_tcp_hits_local_server() -> None:
    """A live local listener over plain TCP must yield a non-negative RTT."""
    async def _noop(reader, writer):
        writer.close()

    server = await asyncio.start_server(_noop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        sample = await sample_connection("127.0.0.1", port, use_tls=False)
    finally:
        server.close()
        await server.wait_closed()

    assert sample.rtt_ms is not None
    assert sample.rtt_ms >= 0.0
    assert sample.tls_ms is None      # plain TCP — no handshake attempted
    assert sample.alpn_protocol is None


async def test_sample_connection_closed_port_all_none() -> None:
    """A refused connection must return an all-None sample, never raise."""
    sample = await sample_connection("127.0.0.1", 1, use_tls=False, timeout_s=1.0)
    assert sample.rtt_ms is None
    assert sample.tls_ms is None


async def test_sample_connection_tls_against_plain_server_degrades() -> None:
    """use_tls against a non-TLS listener: RTT still measured, TLS fields None.

    The TLS handshake fails (the server speaks no TLS), but that must not raise
    and must not lose the RTT already sampled on the plain connect."""
    async def _noop(reader, writer):
        writer.close()

    server = await asyncio.start_server(_noop, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        sample = await sample_connection("127.0.0.1", port, use_tls=True, timeout_s=1.0)
    finally:
        server.close()
        await server.wait_closed()

    assert sample.rtt_ms is not None       # plain connect succeeded
    assert sample.tls_ms is None           # TLS handshake could not complete
    assert sample.h2_supported is None


# =============================================================================
# F. phases_to_dict()
# =============================================================================


def test_phases_to_dict_shape() -> None:
    phases = _base_phases(tls_cert_days_left=20)
    findings = analyze(phases)
    out = phases_to_dict(phases, findings)

    assert set(out.keys()) == {"measured", "derived", "findings", "measured_at"}
    assert out["measured"]["rtt_ms"] == 20.0
    assert out["derived"]["bandwidth_confidence"] == CONFIDENCE_INSUFFICIENT
    assert any(f["code"] == CODE_CERT_EXPIRING for f in out["findings"])


def test_phases_to_dict_preserves_none() -> None:
    """None must serialize as null, not 0 — 'not measured' is not 'zero'."""
    phases = _base_phases(dns_ms=None, rtt_ms=None, goodput_bps=None)
    out = phases_to_dict(phases, [])
    assert out["measured"]["dns_ms"] is None
    assert out["measured"]["rtt_ms"] is None
    assert out["measured"]["goodput_bps"] is None


def test_phases_to_dict_carries_alpn() -> None:
    """ALPN and h2 support must survive serialization for the dashboard/state."""
    out = phases_to_dict(_base_phases(alpn_protocol="h2", h2_supported=True), [])
    assert out["measured"]["alpn_protocol"] == "h2"
    assert out["measured"]["h2_supported"] is True


# =============================================================================
# G. HTTP/2 detection rule
# =============================================================================


def test_http2_unsupported_fires_on_slow_path() -> None:
    """Server declines h2 AND the path is slow → info-level upgrade hint."""
    findings = analyze(_base_phases(
        h2_supported=False, alpn_protocol="http/1.1", rtt_ms=150.0,
        connect_total_ms=160.0, tls_ms=10.0,
    ))
    hits = [f for f in findings if f.code == CODE_HTTP2_UNSUPPORTED]
    assert len(hits) == 1
    assert hits[0].severity == "info"


def test_http2_silent_on_fast_path() -> None:
    """No h2 but a fast path → the HOL penalty is negligible → stay quiet."""
    findings = analyze(_base_phases(h2_supported=False, alpn_protocol="http/1.1", rtt_ms=20.0))
    assert CODE_HTTP2_UNSUPPORTED not in {f.code for f in findings}


def test_http2_silent_when_untested() -> None:
    """h2_supported None (sampler failed) must never fire the rule."""
    findings = analyze(_base_phases(h2_supported=None, alpn_protocol=None, rtt_ms=300.0))
    assert CODE_HTTP2_UNSUPPORTED not in {f.code for f in findings}


# =============================================================================
# G2. Per-target threshold overrides
# =============================================================================


def test_analyze_rtt_high_ms_override_raises_the_bar() -> None:
    """An RTT that trips the global default must stay silent under a higher
    per-target override — the whole point of the override is to stop a
    legitimately-distant target from chronically false-firing HIGH_RTT."""
    phases = _base_phases(rtt_ms=150.0, connect_total_ms=160.0, tls_ms=10.0)
    assert CODE_HIGH_RTT in _codes(analyze(phases))                       # default fires
    assert CODE_HIGH_RTT not in _codes(analyze(phases, rtt_high_ms=250.0))  # override silences it


def test_analyze_rtt_high_ms_override_lowers_the_bar_too() -> None:
    """A tighter override must fire where the global default would stay quiet
    — proves the kwarg actually replaces the threshold, not just raises a floor."""
    phases = _base_phases(rtt_ms=60.0)
    assert CODE_HIGH_RTT not in _codes(analyze(phases))                  # default: 60 < 100, silent
    assert CODE_HIGH_RTT in _codes(analyze(phases, rtt_high_ms=50.0))    # override: 60 > 50, fires


def test_analyze_rtt_high_ms_override_also_gates_http2_rule() -> None:
    """Rule 7 (HTTP2_NOT_SUPPORTED) shares the same physical threshold as
    rule 1 — both concern 'is this RTT high', so a per-target override must
    apply to both, not just the rule it was first written for."""
    phases = _base_phases(rtt_ms=150.0, h2_supported=False, alpn_protocol="http/1.1")
    assert CODE_HTTP2_UNSUPPORTED in _codes(analyze(phases))
    assert CODE_HTTP2_UNSUPPORTED not in _codes(analyze(phases, rtt_high_ms=250.0))


# =============================================================================
# H. degraded_reason() — the DEGRADED policy
# =============================================================================


def test_degraded_reason_none_when_healthy() -> None:
    """A clean, fast probe is not degraded."""
    phases = _base_phases(rtt_ms=20.0, ttfb_ms=40.0)
    assert degraded_reason(phases, analyze(phases)) is None


def test_degraded_reason_fires_on_performance_finding() -> None:
    """A degrading finding (high RTT) marks the service DEGRADED."""
    phases = _base_phases(rtt_ms=300.0, connect_total_ms=310.0, tls_ms=10.0)
    reason = degraded_reason(phases, analyze(phases))
    assert reason is not None
    assert CODE_HIGH_RTT in reason


def test_degraded_reason_fires_on_slow_ttfb_alone() -> None:
    """Even with no finding, a TTFB over the threshold is degradation."""
    phases = _base_phases(rtt_ms=20.0, ttfb_ms=DEGRADED_TTFB_MS + 100.0, server_processing_ms=10.0)
    reason = degraded_reason(phases, [])
    assert reason is not None
    assert "SLOW_RESPONSE" in reason


def test_degraded_reason_ttfb_override_raises_the_bar() -> None:
    """A per-target degraded_ttfb_ms override must stop a legitimately-slow
    report (e.g. a 3s export endpoint) from chronically reading DEGRADED."""
    phases = _base_phases(rtt_ms=20.0, ttfb_ms=3_000.0, server_processing_ms=10.0)
    assert degraded_reason(phases, []) is not None                        # default: fires
    assert degraded_reason(phases, [], degraded_ttfb_ms=5_000.0) is None  # override: silent


def test_degraded_reason_ignores_cert_and_http2() -> None:
    """CERT_EXPIRING and HTTP2_NOT_SUPPORTED are not degradation of the present.

    An expiring cert is a future outage; a missing h2 is an optimization. A
    service that is fast and answering today is not DEGRADED just because one
    of those non-performance hints fired."""
    phases = _base_phases(
        rtt_ms=20.0, ttfb_ms=40.0, tls_cert_days_left=3,          # cert critical
        h2_supported=False, alpn_protocol="http/1.1",             # but fast, so http2 rule won't fire
    )
    findings = analyze(phases)
    # Sanity: the cert finding is present...
    assert any(f.code == CODE_CERT_EXPIRING for f in findings)
    # ...but it must not drive DEGRADED.
    assert degraded_reason(phases, findings) is None


# =============================================================================
# G. cert_alert_threshold() / is_cert_healthy() — the suppression rule
#
# Pure functions on purpose: the whole rule is exercised here with plain
# calls, no probe, no state file, no webhook. The property under test is not
# "does it alert" but "does it stop alerting" — a cert sits below its warning
# threshold for 30 days, and 43,198 of those 43,200 runs must be silent.
# =============================================================================


@pytest.mark.parametrize("days,stored,expected", [
    # --- first crossings -----------------------------------------------------
    (25,   None, CERT_WARN_DAYS),   # crosses warn for the first time
    (6,    None, CERT_CRIT_DAYS),   # first observation already critical
    # --- suppression: the whole point of the feature -------------------------
    (24,   30,   None),             # still in the warn band, already said so
    (8,    30,   None),             # bottom of the warn band, still silent
    (5,    7,    None),             # still critical, already said so
    (1,    7,    None),             # about to expire, still no repeat
    # --- escalation ----------------------------------------------------------
    (6,    30,   CERT_CRIT_DAYS),   # warn -> critical must NOT be suppressed
    # --- healthy -------------------------------------------------------------
    (90,   None, None),
    (90,   7,    None),             # renewed; the clearing is is_cert_healthy's job
    (31,   None, None),             # one day above the threshold: silence
    (30,   None, None),             # exactly at the threshold: not below it
    # --- no certificate ------------------------------------------------------
    (None, None, None),
    (None, 30,   None),
])
def test_cert_alert_threshold_cases(days, stored, expected) -> None:
    assert cert_alert_threshold(days, stored) == expected


@pytest.mark.parametrize("days,healthy", [
    (90,   True),
    (30,   True),    # at the threshold counts as healthy — nothing to warn about
    (29,   False),
    (1,    False),
    (None, False),   # no certificate is not the same as a healthy one
])
def test_is_cert_healthy(days, healthy) -> None:
    assert is_cert_healthy(days) is healthy


def test_partial_renewal_into_the_warn_band_does_not_realert() -> None:
    """Documented limitation of the design note, asserted so it stays a decision
    rather than drifting into an accident: a cert renewed from 5 days to 25
    stays silent, because the operator already got the critical."""
    assert cert_alert_threshold(25, CERT_CRIT_DAYS) is None


@pytest.mark.parametrize("days,last_seen,renewed", [
    (25,   3,    True),    # the audit's case: renewed but still under 30
    (90,   5,    True),
    (6,    5,    True),    # even a tiny rise is a replacement
    (24,   25,   False),   # ordinary decay
    (25,   25,   False),   # same day, no movement
    (25,   None, False),   # first observation is not a renewal
    (None, 25,   False),   # lost TLS visibility is not a renewal
    (None, None, False),
])
def test_is_cert_renewed(days, last_seen, renewed) -> None:
    """Certificate lifetime only decreases with the calendar, so a RISE is
    the one unambiguous replacement signal — and the only one available when
    the new certificate is itself under CERT_WARN_DAYS."""
    assert is_cert_renewed(days, last_seen) is renewed
