"""build-grafana-dashboards.py — generate the Grafana dashboards from the target list.

What these dashboards are for
-----------------------------
Analysis, not monitoring. Current status, uptime and the live view belong to
dashboard.py, which serves them from the store; `--report` prints the same
numbers in a terminal. Grafana is here for the one thing neither of those can
do: show the SHAPE of a distribution over time. The per-target backend
calibration turned entirely on seeing that cloudflare's MEDIAN backend time sat above the
threshold, and that is invisible in a table of numbers.

That is why this ships behind the compose profile named "analysis", why nothing
here alerts, and why there is no status dashboard: a second, staler answer to a
question dashboard.py already answers is not worth maintaining.

Why generated rather than hand-written
--------------------------------------
A Grafana panel names its series in SQL. With targets typed into the queries by
hand, adding a target to the monitor leaves it invisible in the graphs, and
nothing fails: the panel keeps drawing the old series and looks perfectly
healthy. It is the same class of desynchronisation that left the Dockerfile
copying three of six modules for five releases.

The source is the same list main.py probes — `data/targets.json` when it exists,
`targets.yaml` as the seed. Reading only the YAML is how that failure came back
in: the store has been authoritative since the dashboard took over the list, so
a target added from the dashboard never reached the graphs and one removed from
it left a frozen series that reads as healthy. Regenerating is one command, and the regression test
compares what is generated against what is committed: add a target without
regenerating and the test fails in that same commit.

Panel titles and descriptions are interface text, read by whoever operates the
tool. They are English, like everything else in the repository.

Usage:
    python scripts/build-grafana-dashboards.py
    python scripts/build-grafana-dashboards.py --check   # verify only, no writes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "grafana" / "dashboards"

DS = {"type": "frser-sqlite-datasource", "uid": "vigil-sqlite"}

# The time axis is run_started_at, NOT checked_at.
#
# All four targets in a run share run_started_at, but each finishes in its own
# second: checked_at scatters by up to 6 s within the same run. Grouped by
# checked_at, 920 rows produce 505 distinct timestamps, each carrying one target
# with the other three NULL -- broken-up graphs that read as missing data. With
# run_started_at it is 230 dense rows of 4 values; verified as 230 groups, all
# of exactly 4 rows.
#
# Percentiles and aggregates still use checked_at, which is when the
# measurement actually happened.
#
# SQLite parses the '2026-08-03T17:31:32Z' format and returns epoch seconds;
# verified with a datetime round trip before a single query was written.
TIME = "CAST(strftime('%s', run_started_at) AS INTEGER) AS time"

# The thresholds that decide DEGRADED, copied from diagnostics.py. Duplicated
# here deliberately, with a test comparing the two: a calibration dashboard
# drawing a threshold line different from the one the code applies is worse than
# no dashboard at all, because it looks like a measurement.
THRESHOLDS = {
    "dns_ms": 200.0,
    "ttfb_ms": 1_500.0,
    "server_processing_ms": 500.0,
}


def short(url: str) -> str:
    """Readable series name. 'https://www.google.com' -> 'google.com'."""
    name = url.split("://", 1)[-1].split("/", 1)[0]
    return name[4:] if name.startswith("www.") else name


def load_targets() -> list[str]:
    """The list actually being probed — the store first, the YAML as the seed.

    Same precedence as main.load_targets, and it has to be. This script read
    only targets.yaml, which stopped being the source of truth once the
    dashboard took over the list: a target added from the dashboard never
    appeared in the graphs, and one removed from the dashboard left its panel
    drawing the last series it ever had -- frozen, and indistinguishable from
    "everything is normal" for anyone who does not check the timestamp of the
    final point.

    That is the exact failure this script was written to prevent, reintroduced
    by reading the wrong file. A dashboard that lies quietly is worse than no
    dashboard.
    """
    sys.path.insert(0, str(ROOT))
    import targetstore

    # A store that exists and cannot be used raises, and that is not "no store".
    # Falling through to the YAML would draw graphs for a list the monitor is not
    # running, so the generator stops with the reason instead. The name is looked
    # up here, when it runs, and an empty tuple catches nothing: a targetstore
    # that predates StoreError behaves as it always did.
    try:
        stored = targetstore.read_store()
    except getattr(targetstore, "StoreError", ()) as exc:
        raise SystemExit(
            "build-grafana-dashboards: the target store is present but unusable "
            f"({' '.join(str(exc).split())}); not falling back to targets.yaml"
        ) from None
    if stored is not None:
        out = [e["url"] for e in stored]
        if out:
            return out

    doc = yaml.safe_load((ROOT / "targets.yaml").read_text(encoding="utf-8"))
    out = []
    for entry in doc.get("targets") or []:
        out.append(entry["url"] if isinstance(entry, dict) else entry)
    if not out:
        raise SystemExit("targets.yaml declares no targets")
    return out


def pivot(urls: list[str], column: str, agg: str = "AVG") -> str:
    """One column per target — wide format.

    Grafana draws one series per column with no extra configuration. The
    alternative (long format with a 'metric' column) depends on how each
    datasource decides to group, and that behaviour is not the same across them.
    """
    cols = ",\n  ".join(
        f'{agg}(CASE WHEN url = {sql_str(u)} THEN {column} END) AS {sql_id(short(u))}'
        for u in urls
    )
    return f"SELECT\n  {TIME},\n  {cols}\nFROM probe_results\nGROUP BY time\nORDER BY time"


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_id(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def target(sql: str, fmt: str = "time series") -> dict:
    return {
        "refId": "A",
        "datasource": DS,
        "queryText": sql,
        "rawQueryText": sql,
        "queryType": fmt,
        "timeColumns": ["time"] if fmt == "time series" else [],
        "format": fmt,
    }


def panel(pid: int, title: str, ptype: str, sql: str, *, x: int, y: int,
          w: int = 12, h: int = 8, unit: str | None = None,
          fmt: str = "time series", description: str = "",
          extra: dict | None = None) -> dict:
    defaults: dict = {"custom": {}}
    if unit:
        defaults["unit"] = unit
    p = {
        "id": pid,
        "type": ptype,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [target(sql, fmt)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }
    if extra:
        _merge(p, extra)
    return p


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def dashboard(uid: str, title: str, description: str, panels: list) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["vigil-sre"],
        "timezone": "browser",
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
        "refresh": "",
        # Relative, so the range describes whatever database is mounted. A fixed
        # window made every installation open on one particular fortnight, and
        # on anyone else's data the panels came out empty for a reason that had
        # nothing to do with their data. A history older than a week needs the
        # range widened; the dashboard descriptions say so.
        "time": {"from": "now-7d", "to": "now"},
        "panels": panels,
    }


# =============================================================================
# 1. Phase timings — where the milliseconds go
# =============================================================================

def build_phases(urls: list[str]) -> dict:
    def percentiles(column: str) -> str:
        return (
            "WITH r AS (\n"
            f"  SELECT url, {column} AS v,\n"
            f"         CUME_DIST() OVER (PARTITION BY url ORDER BY {column}) AS cd\n"
            f"  FROM probe_results WHERE {column} IS NOT NULL\n)\n"
            "SELECT url AS target, COUNT(*) AS n,\n"
            "  ROUND(MIN(CASE WHEN cd >= 0.50 THEN v END)) AS p50,\n"
            "  ROUND(MIN(CASE WHEN cd >= 0.95 THEN v END)) AS p95,\n"
            "  ROUND(MIN(CASE WHEN cd >= 0.99 THEN v END)) AS p99,\n"
            "  ROUND(MAX(v)) AS max\n"
            "FROM r GROUP BY url ORDER BY p95 DESC"
        )

    panels = [
        panel(1, "TTFB — time to first byte", "timeseries",
              pivot(urls, "ttfb_ms"), x=0, y=0, unit="ms",
              description="The measure closest to 'how long did it take'."),
        panel(2, "RTT — TCP round trip", "timeseries",
              pivot(urls, "rtt_ms"), x=12, y=0, unit="ms",
              description=(
                  "Physical distance. Server tuning does not improve it; "
                  "moving the content closer does.")),
        panel(3, "DNS resolution", "timeseries",
              pivot(urls, "dns_ms"), x=0, y=8, unit="ms"),
        panel(4, "TLS handshake", "timeseries",
              pivot(urls, "tls_ms"), x=12, y=8, unit="ms",
              description="New connections only; reused ones do not pay it."),
        panel(5, "Backend processing  (TTFB − RTT)", "timeseries",
              pivot(urls, "server_processing_ms"), x=0, y=16, w=24, unit="ms",
              description=(
                  "Null when there were redirects: phase attribution is not "
                  "valid across hops, so it is reported empty instead of "
                  "made up.")),
        panel(6, "TTFB percentiles (ms)", "table", percentiles("ttfb_ms"),
              x=0, y=24, w=12, h=8, fmt="table",
              description="Nearest-rank with CUME_DIST — the same method as --report."),
        panel(7, "Backend percentiles (ms)", "table",
              percentiles("server_processing_ms"),
              x=12, y=24, w=12, h=8, fmt="table",
              description=(
                  "The same percentiles over backend processing time "
                  "(TTFB minus RTT). Redirected probes carry no value and "
                  "are left out, not counted as zero.")),
    ]
    return dashboard(
        "vigil-phases", "vigil-sre · Latency phases",
        "Where the time goes: DNS, TCP, TLS, backend, transfer. Opens on the "
        "last 7 days; widen the range if the mounted database holds older "
        "probes. The percentile tables cover every probe in the mounted "
        "database, whatever the range.",
        panels)


# =============================================================================
# 3. Threshold calibration — the dashboard that answers a decision
# =============================================================================

#: Candidate backend thresholds to evaluate, in ms. The first one is the value
#: in force today, so the table shows the starting point beside the alternatives
#: instead of asking the reader to remember it.
BACKEND_CANDIDATES = (500, 750, 1_000, 1_500, 2_000, 3_000)


def build_calibration(urls: list[str]) -> dict:
    cols = ",\n".join(
        f"  ROUND(100.0 * SUM(CASE WHEN server_processing_ms > {c} THEN 1 ELSE 0 END)\n"
        f"        / COUNT(*), 1) AS {sql_id(f'{c} ms' + (' (current)' if c == BACKEND_CANDIDATES[0] else ''))}"
        for c in BACKEND_CANDIDATES
    )
    firing = (
        "SELECT\n  url AS target,\n  COUNT(*) AS samples,\n"
        f"{cols}\n"
        "FROM probe_results\nWHERE server_processing_ms IS NOT NULL\n"
        "GROUP BY url ORDER BY url"
    )

    median = (
        "WITH r AS (\n"
        "  SELECT url, server_processing_ms AS v,\n"
        "         CUME_DIST() OVER (PARTITION BY url ORDER BY server_processing_ms) AS cd\n"
        "  FROM probe_results WHERE server_processing_ms IS NOT NULL\n)\n"
        "SELECT url AS target,\n"
        "  ROUND(MIN(CASE WHEN cd >= 0.50 THEN v END)) AS p50,\n"
        f"  {THRESHOLDS['server_processing_ms']:.0f} AS current_threshold,\n"
        "  ROUND(MIN(CASE WHEN cd >= 0.50 THEN v END))\n"
        f"    - {THRESHOLDS['server_processing_ms']:.0f} AS margin\n"
        "FROM r GROUP BY url ORDER BY margin DESC"
    )

    return dashboard(
        "vigil-calibration", "vigil-sre · Threshold calibration",
        "What share of each target's probes each candidate backend threshold "
        "would flag, computed over every probe in the mounted database. The "
        "time-series panel opens on the last 7 days; widen the range if the "
        "mounted database holds older probes.",
        [
            panel(1, "Backend: % of probes that would exceed each threshold", "table",
                  firing, x=0, y=0, w=24, h=8, fmt="table",
                  description=(
                      "Each cell is the firing rate if that value were the "
                      "threshold, over every probe in the mounted database "
                      "that has a backend time. The "
                      f"'{BACKEND_CANDIDATES[0]} ms (current)' column is what "
                      "the monitor applies by default: a target whose rate "
                      "there is high is being flagged for its normal "
                      "operation, not for an anomaly."),
                  extra={"fieldConfig": {"defaults": {
                      "unit": "percent",
                      "custom": {"cellOptions": {"type": "color-background"}},
                      "thresholds": {"mode": "absolute", "steps": [
                          {"color": "green", "value": None},
                          {"color": "orange", "value": 5},
                          {"color": "red", "value": 20}]}}}}),

            panel(2, "Backend median against the current threshold", "table",
                  median, x=0, y=8, w=12, h=8, fmt="table",
                  description=(
                      "Each target's median backend time over every probe in "
                      "the mounted database, minus the default threshold "
                      f"({THRESHOLDS['server_processing_ms']:.0f} ms). A "
                      "positive margin means the threshold sits BELOW the "
                      "target's median: it does not measure an anomaly, it "
                      "measures normal operation. A threshold like that "
                      "cannot be silenced without turning the whole rule off. "
                      "A target that sets degraded_backend_ms is judged "
                      "against its own value, not this one."),
                  extra={"fieldConfig": {"overrides": [{
                      "matcher": {"id": "byName", "options": "margin"},
                      "properties": [
                          {"id": "custom.cellOptions",
                           "value": {"type": "color-background"}},
                          {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                              {"color": "green", "value": None},
                              {"color": "red", "value": 0}]}}]}]}}),

            panel(3, "Backend time distribution", "histogram",
                  pivot(urls, "server_processing_ms"),
                  x=12, y=8, w=12, h=8, unit="ms",
                  description=(
                      "How each target's backend times are distributed. If a "
                      "target's mass sits to the right of the threshold, the "
                      "threshold does not fit that target.")),

            panel(4, "Backend over time, with the current threshold marked",
                  "timeseries", pivot(urls, "server_processing_ms"),
                  x=0, y=16, w=24, h=9, unit="ms",
                  description=(
                      "The red line is the default backend threshold, "
                      "TTFB_BACKEND_SLACK_MS = "
                      f"{THRESHOLDS['server_processing_ms']:.0f} in "
                      "diagnostics.py. A probe above it is flagged "
                      "SLOW_BACKEND unless its target sets "
                      "degraded_backend_ms."),
                  extra={"fieldConfig": {"defaults": {
                      "custom": {"thresholdsStyle": {"mode": "line"}},
                      "thresholds": {"mode": "absolute", "steps": [
                          {"color": "transparent", "value": None},
                          {"color": "red",
                           "value": THRESHOLDS["server_processing_ms"]}]}}}}),
        ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Write nothing: fail if what is committed does "
                             "not match what targets.yaml produces today.")
    args = parser.parse_args()

    urls = load_targets()
    # No status dashboard. It drew current status and uptime, which dashboard.py
    # already serves live and from the store -- a second, staler answer to a
    # question that was already answered. Grafana earns its place here for the
    # one thing neither the terminal report nor the built-in dashboard can do:
    # show the SHAPE of a distribution over time. Seeing that cloudflare's
    # median backend time sat ABOVE the threshold is what the whole per-target
    # backend calibration turned on, and it is invisible in a table.
    #
    # So this is an ANALYSIS surface, not a monitoring one. That is why it
    # lives behind the compose profile named "analysis" and why nothing here
    # alerts.
    built = {
        "phases.json": build_phases(urls),
        "calibration.json": build_calibration(urls),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, doc in built.items():
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        path = OUT / name
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8")
            print(f"  {path.relative_to(ROOT)}  ({len(doc['panels'])} panels)")

    if args.check:
        if stale:
            print("  out of date: " + ", ".join(stale), file=sys.stderr)
            print("  run: python scripts/build-grafana-dashboards.py",
                  file=sys.stderr)
            return 1
        print(f"  {len(built)} dashboards in sync with the target list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
