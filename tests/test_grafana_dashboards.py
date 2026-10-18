"""The Grafana dashboards: that they stay in sync, that their queries run, and
that the threshold lines they draw are the ones the code applies.

None of these failures is visible from inside Grafana. A panel whose query
returns no rows looks identical to one whose datasource is down; a panel
drawing a stale threshold line looks exactly like a correct measurement. So
they are asserted here rather than eyeballed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GEN = ROOT / "scripts" / "build-grafana-dashboards.py"
DASH_DIR = ROOT / "grafana" / "dashboards"
PROV_DS = ROOT / "grafana" / "provisioning" / "datasources" / "sqlite.yml"

from history import SCHEMA  # noqa: E402

COLUMNS = (
    "run_started_at", "url", "checked_at", "status", "error", "http_status",
    "rtt_ms", "dns_ms", "connect_total_ms", "tls_ms", "ttfb_ms",
    "server_processing_ms", "transfer_ms", "body_bytes", "goodput_bps",
)


def dashboards() -> list[tuple[str, dict]]:
    return [(p.name, json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(DASH_DIR.glob("*.json"))]


def panels():
    for name, doc in dashboards():
        for panel in doc["panels"]:
            yield name, panel


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Path:
    """A database shaped like the real one: 4 targets, several runs, NULLs.

    The NULLs matter. tls_ms is absent when a connection is reused and
    server_processing_ms is nulled on redirects, so a database without them
    would let through a query that breaks against the real data.
    """
    db = tmp_path_factory.mktemp("grafana") / "seed.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    urls = [e["url"] if isinstance(e, dict) else e
            for e in yaml.safe_load(
                (ROOT / "targets.yaml").read_text(encoding="utf-8"))["targets"]]
    rows = []
    for run in range(8):
        started = f"2026-08-{3 + run:02d}T10:00:00Z"
        for i, url in enumerate(urls):
            rows.append((
                started, url, f"2026-08-{3 + run:02d}T10:00:0{i}Z",
                "DOWN" if (run + i) % 7 == 0 else
                ("DEGRADED" if (run + i) % 3 == 0 else "UP"),
                None, 200,
                40.0 + i * 10, 2.0 + i, 60.0 + i, None if i == 1 else 20.0 + i,
                120.0 + run * 50, None if i == 2 else 300.0 + run * 90,
                80.0, 1000, 5000.0,
            ))
    with con:
        con.executemany(
            f"INSERT INTO probe_results ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(COLUMNS))})", rows)
    con.close()
    return db


# =============================================================================
# A. The queries run and return something
# =============================================================================

@pytest.mark.parametrize("name,panel", list(panels()),
                         ids=[f"{n}:{p['id']}" for n, p in panels()])
def test_every_panel_query_runs_and_returns_rows(name, panel, seeded) -> None:
    """A broken query and an empty one look the same in Grafana: a grey panel.

    Neither fails anywhere — Grafana draws the panel, reports nothing, and the
    first hypothesis of whoever looks at it is that the datasource lost its
    connection.
    """
    sql = panel["targets"][0]["queryText"]
    con = sqlite3.connect(f"file:{seeded.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    assert rows, f"{name} panel {panel['id']} ({panel['title']}) returns no rows"


def test_the_time_axis_groups_by_run_not_by_probe() -> None:
    """checked_at scatters the 4 targets across different seconds.

    Grouped by it, each row carries one target and three NULLs: the graph comes
    out broken up and reads as missing data. run_started_at is the only value
    all four probes of a run share.
    """
    series = [p for _, p in panels()
              if p["targets"][0]["queryType"] == "time series"]
    assert series, "no time-series panel is left"
    for panel in series:
        sql = panel["targets"][0]["queryText"]
        assert "strftime('%s', run_started_at)" in sql, (
            f"panel {panel['id']} ({panel['title']}) uses a different time axis")


def test_the_series_are_dense_enough_to_read(seeded) -> None:
    """Wide format has to carry values, not a grid of NULLs."""
    con = sqlite3.connect(f"file:{seeded.as_posix()}?mode=ro", uri=True)
    try:
        for _, panel in panels():
            if panel["targets"][0]["queryType"] != "time series":
                continue
            rows = con.execute(panel["targets"][0]["queryText"]).fetchall()
            cells = sum(len(r) - 1 for r in rows)
            filled = sum(1 for r in rows for v in r[1:] if v is not None)
            # 45% tolerates tls_ms (reused connections) and
            # server_processing_ms (redirects), NULL by design rather than by
            # mistake.
            assert filled / cells >= 0.45, (
                f"panel {panel['id']} ({panel['title']}): only "
                f"{100 * filled / cells:.0f}% of cells carry a value")
    finally:
        con.close()


# =============================================================================
# B. Drift — the failures you cannot see from inside Grafana
# =============================================================================

def _load_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_dash", GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_dashboards_match_targets_yaml() -> None:
    """Adding a target without regenerating leaves it invisible in the graphs.

    And nothing fails: the panels keep drawing the old series and look
    perfectly healthy. It is the same desynchronisation that left the
    Dockerfile copying three of six modules for five releases — a file that
    should have changed and did not appears in no diff.
    """
    result = subprocess.run(
        [sys.executable, str(GEN), "--check"],
        cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0, (
        "the committed dashboards do not match targets.yaml:\n"
        f"{result.stdout}{result.stderr}")


def test_check_actually_detects_a_new_target(tmp_path, monkeypatch) -> None:
    """The guard above is worth nothing unless it knows how to fail.

    A --check that returns 0 no matter what offers the same reassurance as one
    that works, and this project has already been bitten by that three times.
    """
    gen = _load_generator()
    urls = gen.load_targets()
    before = json.dumps(gen.build_phases(urls), sort_keys=True)
    after = json.dumps(
        gen.build_phases(urls + ["https://new.example.com"]), sort_keys=True)
    assert before != after, (
        "adding a target does not change the dashboard — the generator is not "
        "reading it, and --check cannot detect anything")
    assert "new.example.com" in after


def test_the_generator_reads_the_store_not_just_the_seed(tmp_path, monkeypatch) -> None:
    """The graphs must describe the list that is actually probed.

    This script read only targets.yaml, which stopped being the source of truth
    once the dashboard took over the list. So a target added from the dashboard never reached the graphs,
    and one removed from it left its panel drawing the last series it ever had —
    frozen, and indistinguishable from "everything is normal" to anyone who does
    not check the timestamp of the final point.

    That is the same failure this script exists to prevent, reintroduced by
    reading the wrong file. Without this test the fix reverts silently.
    """
    import targetstore

    gen   = _load_generator()
    store = tmp_path / "targets.json"
    targetstore.write_store(
        [targetstore.validate_entry({"url": "https://store-only.example"})], store)
    monkeypatch.setattr(targetstore, "STORE_FILE", store)

    assert gen.load_targets() == ["https://store-only.example"], (
        "the generator ignored the store and fell back to the YAML seed"
    )


def test_a_present_but_unusable_store_stops_the_generator_and_is_not_the_seed(
        monkeypatch) -> None:
    """read_store raises StoreError for a store that exists and cannot be used.
    The generator must stop with one line naming the reason. Falling back to
    targets.yaml would draw graphs for a list the monitor is not running, from a
    file that stopped being read the day the store was written -- the failure
    this script exists to prevent, caused by swallowing the one signal that says
    so.

    StoreError does not exist on every revision of targetstore, so the test
    assigns its own class to that name; the generator looks the name up when it
    runs rather than when it is imported."""
    import targetstore

    class BrokenStore(Exception):
        pass

    def unusable(*_args, **_kwargs):
        # Two lines on purpose: the exit message has to flatten them.
        raise BrokenStore("data/targets.json is not valid JSON\n  (line 3)")

    monkeypatch.setattr(targetstore, "StoreError", BrokenStore, raising=False)
    monkeypatch.setattr(targetstore, "read_store", unusable)
    gen = _load_generator()
    monkeypatch.setattr(gen.yaml, "safe_load", lambda *_a, **_k: pytest.fail(
        "the generator read targets.yaml after the store was refused"))

    with pytest.raises(SystemExit) as raised:
        gen.load_targets()

    reason = raised.value.code
    assert isinstance(reason, str), "a non-string exit code would not print the reason"
    assert "not valid JSON" in reason
    assert "\n" not in reason, "the reason must be one line"
    assert "Traceback" not in reason


def test_main_exits_1_with_the_reason_for_an_unusable_store(monkeypatch, capsys) -> None:
    """Through main(), the way the command line runs it: status 1 and the reason
    on stderr, no dashboards written."""
    import targetstore

    class BrokenStore(Exception):
        pass

    def unusable(*_args, **_kwargs):
        raise BrokenStore("data/targets.json is not valid JSON (line 3)")

    monkeypatch.setattr(targetstore, "StoreError", BrokenStore, raising=False)
    monkeypatch.setattr(targetstore, "read_store", unusable)
    gen = _load_generator()
    monkeypatch.setattr(sys, "argv", ["build-grafana-dashboards.py"])
    written: list = []
    monkeypatch.setattr(Path, "write_text",
                        lambda self, *a, **k: written.append(self))

    with pytest.raises(SystemExit) as raised:
        gen.main()

    assert raised.value.code not in (0, None)
    assert not written, "dashboards were written from a list that was refused"


def test_an_error_that_is_not_a_store_error_is_not_swallowed(monkeypatch) -> None:
    """The catch is for the store's own error and nothing broader. On a
    targetstore without StoreError it must catch nothing at all."""
    import targetstore

    monkeypatch.delattr(targetstore, "StoreError", raising=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("a bug, not a bad store")

    monkeypatch.setattr(targetstore, "read_store", broken)
    gen = _load_generator()

    with pytest.raises(RuntimeError, match="a bug"):
        gen.load_targets()


def test_the_threshold_lines_match_the_code_that_applies_them() -> None:
    """A threshold drawn differently from the one applied is worse than none.

    The panel looks like a measurement: someone reads "this is below the line"
    and concludes it will not alert, while the code uses a different number. A
    wrong dashboard does not look wrong.
    """
    import diagnostics
    gen = _load_generator()
    expected = {
        "dns_ms": diagnostics.DNS_SLOW_MS,
        "ttfb_ms": diagnostics.DEGRADED_TTFB_MS,
        "server_processing_ms": diagnostics.TTFB_BACKEND_SLACK_MS,
    }
    assert gen.THRESHOLDS == expected, (
        f"the dashboards draw {gen.THRESHOLDS} while diagnostics.py applies "
        f"{expected}")


def test_the_current_threshold_is_first_among_the_candidates() -> None:
    """The calibration table compares alternatives against the current state.

    If the first candidate stops being the value in force, the column labelled
    "(current)" lies and the whole comparison loses its reference point.
    """
    import diagnostics
    gen = _load_generator()
    assert gen.BACKEND_CANDIDATES[0] == diagnostics.TTFB_BACKEND_SLACK_MS, (
        "the first candidate is no longer the threshold in force")
    doc = json.loads((DASH_DIR / "calibration.json").read_text(encoding="utf-8"))
    sql = doc["panels"][0]["targets"][0]["queryText"]
    assert "(current)" in sql and str(int(diagnostics.TTFB_BACKEND_SLACK_MS)) in sql


def test_every_panel_points_at_the_provisioned_datasource() -> None:
    """The dashboards reference a datasource by uid.

    If the provisioned uid changes, the panels open with no data source and it
    has to be reassigned by hand, one at a time, in every installation.
    """
    prov = yaml.safe_load(PROV_DS.read_text(encoding="utf-8"))
    uids = {d["uid"] for d in prov["datasources"]}
    types = {d["type"] for d in prov["datasources"]}
    for name, panel in panels():
        ds = panel["datasource"]
        assert ds["uid"] in uids, f"{name} panel {panel['id']}: uid {ds['uid']}"
        assert ds["type"] in types, f"{name} panel {panel['id']}: type {ds['type']}"


def test_the_dashboards_open_on_a_relative_range() -> None:
    """The range is relative, so it describes whatever database is mounted.

    A fixed window made every installation open on one particular fortnight: on
    anyone else's data the panels came out empty for a reason that has nothing
    to do with their data, and the first conclusion was that the connection
    failed. "Last 7 days" is the default Grafana itself offers for a history
    that is still being written.
    """
    assert dashboards(), "no dashboards were generated"
    for name, doc in dashboards():
        assert doc["time"] == {"from": "now-7d", "to": "now"}, f"{name}: {doc['time']}"


_MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
           "november|december")


def _interface_text() -> list[tuple[str, str]]:
    """Every string a person reads in the generated dashboards: titles and
    descriptions, at dashboard and panel level. SQL is not interface text and
    is full of legitimate numbers."""
    found: list[tuple[str, str]] = []
    for name, doc in dashboards():
        found.append((f"{name} dashboard title", doc["title"]))
        found.append((f"{name} dashboard description", doc.get("description", "")))
        for panel in doc["panels"]:
            found.append((f"{name} panel {panel['id']} title", panel["title"]))
            found.append((f"{name} panel {panel['id']} description",
                          panel.get("description", "")))
    return found


def test_the_descriptions_quote_no_figures_from_a_particular_dataset() -> None:
    """A panel description is read as a statement about the data in front of the
    reader. "On cloudflare.com it fires 63.8% of the time, over 920 probes from
    3 to 17 August" was a measurement of one collection, printed under every
    installation's own numbers, where it reads as a claim about theirs and goes
    on being believed after it stops being true. A description says what the
    panel computes; the panel shows the figure."""
    text = _interface_text()
    assert any(body for _, body in text), "no descriptions to check"
    patterns = {
        "a year": r"\b(19|20)\d{2}\b",
        "a month name": rf"\b({_MONTHS})\b",
        "an ISO date": r"\b\d{4}-\d{2}-\d{2}\b",
        "a percentage": r"\d+(?:\.\d+)?\s?%",
        "a probe count": r"\b\d[\d,]*\s+(?:real\s+)?(?:probes|samples|measurements|rows)\b",
        "a named host": r"\b(?:cloudflare|google|github|httpbin)\b",
    }
    for where, body in text:
        for what, pattern in patterns.items():
            match = re.search(pattern, body, re.IGNORECASE)
            assert not match, f"{where} contains {what}: {match.group(0)!r} in {body!r}"


def test_every_panel_says_what_it_computes() -> None:
    """The cure for a frozen figure is not silence. Each panel that draws a
    derived number (a percentile, a firing rate, a margin) keeps a description,
    and the dashboards say how far back they look and over what."""
    for name, doc in dashboards():
        assert doc["description"].strip(), f"{name} has no dashboard description"
        assert "7 days" in doc["description"], (
            f"{name} does not say what its default range is")
    for name, panel in panels():
        if panel["type"] == "table":
            assert panel["description"].strip(), (
                f"{name} panel {panel['id']} ({panel['title']}) has no description")


def test_the_panels_declare_their_time_column() -> None:
    """Without timeColumns the plugin hands the epoch back as an ordinary
    number and the X axis comes out numbered instead of dated."""
    for name, panel in panels():
        t = panel["targets"][0]
        if t["queryType"] == "time series":
            assert t["timeColumns"] == ["time"], f"{name} panel {panel['id']}"


def test_the_dashboards_describe_the_targets_actually_being_probed() -> None:
    """targets.yaml stopped being the live list when the dashboard took over.

    Since the dashboard took over the list the monitor reads data/targets.json
    and the YAML is only a seed. The dashboards are generated from the YAML —
    correct for a fresh clone, which has no store — but if the local store
    diverges, the graphs name series belonging to a different system than the
    one running here.

    Cannot be asserted in CI: data/ is gitignored and does not exist there.
    This test speaks to the machine that does have a store.
    """
    store_path = ROOT / "data" / "targets.json"
    if not store_path.exists():
        pytest.skip("no local store — the YAML is the live list")

    import targetstore
    store = {e["url"] for e in (targetstore.read_store(store_path) or [])}
    doc = yaml.safe_load((ROOT / "targets.yaml").read_text(encoding="utf-8"))
    seed = {e["url"] if isinstance(e, dict) else e for e in doc["targets"]}

    assert store == seed, (
        "the store and targets.yaml differ, so the dashboards describe a "
        f"different list than the one being probed.\n  store only: "
        f"{sorted(store - seed)}\n  YAML only : {sorted(seed - store)}\n"
        "Sync the two, or generate the dashboards from the store.")
