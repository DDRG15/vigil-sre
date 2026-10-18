"""Daily summaries describe retained observations, never inferred deliveries."""
from __future__ import annotations

import csv
import io
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

import api
import history_browser as browser
from history import SCHEMA

URL = "https://service.example/"


@pytest.fixture
def data(tmp_path):
    db = tmp_path / "history.db"
    day = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    with closing(sqlite3.connect(db)) as con, con:
        con.executescript(SCHEMA)

    def add(status="UP", latency=None, clock="00:00:00", url=URL, date=day):
        stamp = f"{date}T{clock}Z"
        with closing(sqlite3.connect(db)) as con, con:
            con.execute(
                "INSERT INTO probe_results(run_started_at,url,checked_at,status,ttfb_ms) "
                "VALUES(?,?,?,?,?)", (stamp, url, stamp, status, latency))

    selection = browser.filters({"start": [day], "end": [day]})
    return db, day, add, selection


def test_counts_percentiles_and_sampling_gap(data):
    db, day, add, selection = data
    add("UP", 100, "00:00:00")
    add("DEGRADED", 900, "00:01:00")
    add("DOWN", None, "00:03:00")
    # A failed probe awaiting DOWN confirmation can retain recorded UP.
    add("UP", None, "00:04:00")
    add("UNKNOWN", None, "00:05:00")
    row, = browser.daily_summaries(db, selection)
    assert row == {
        "day_utc": day, "url": URL, "observations": 5,
        "up_samples": 2, "slow_samples": 1, "down_samples": 1,
        "other_samples": 1, "recorded_available_pct": 75.0,
        "ttfb_samples": 2, "p50_ttfb_ms": 100.0, "p95_ttfb_ms": 900.0,
        "first_checked_at": day + "T00:00:00Z",
        "last_checked_at": day + "T00:05:00Z", "max_observed_gap_s": 120.0,
    }


@pytest.mark.parametrize("values,p50,p95", [([7], 7, 7), ([10, 10], 10, 10),
    (list(range(1, 11)), 5, 10), (list(range(1, 21)), 10, 19)])
def test_nearest_rank_percentiles(data, values, p50, p95):
    db, _, add, selection = data
    for value in values:
        add(latency=value)
    row, = browser.daily_summaries(db, selection)
    assert (row["p50_ttfb_ms"], row["p95_ttfb_ms"], row["ttfb_samples"]) == (p50, p95, len(values))


def test_missing_invalid_timings_and_unknown_states_are_not_success(data):
    db, _, add, selection = data
    for value in (None, -1, float("inf"), "bad"):
        add("UNKNOWN", value)
    row, = browser.daily_summaries(db, selection)
    assert row["recorded_available_pct"] is None
    assert row["other_samples"] == 4
    assert row["ttfb_samples"] == 0
    assert row["p50_ttfb_ms"] is None and row["p95_ttfb_ms"] is None


def test_zero_latency_and_zero_gap_are_real_measurements(data):
    db, _, add, selection = data
    add(latency=0)
    add(latency=0)
    row, = browser.daily_summaries(db, selection)
    assert row["ttfb_samples"] == 2 and row["p95_ttfb_ms"] == 0
    assert row["max_observed_gap_s"] == 0


def test_a_gap_is_measured_within_one_target_never_across_targets(data):
    """Another target checked in between must not shorten a target's gap: A at
    00:00 and 00:10 went unobserved for 600 s even though B was checked at
    00:00:30. Measured across targets it would read 570 s and hide the hole."""
    db, day, add, selection = data
    other = "https://other.example/"
    add(latency=10, clock="00:00:00")
    add(latency=10, clock="00:00:30", url=other)
    add(latency=10, clock="00:10:00")
    rows = {r["url"]: r for r in browser.daily_summaries(db, selection)}
    assert rows[URL]["max_observed_gap_s"] == 600
    assert rows[other]["max_observed_gap_s"] is None  # its only check in the selection


def test_days_targets_and_midnight_gap_stay_separate(data):
    db, day, add, selection = data
    tomorrow = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    add(latency=10, clock="23:59:00")
    add("DOWN", clock="00:01:00", date=tomorrow)
    add(latency=800, url="https://other.example/")
    selection["until"] = tomorrow + "T23:59:59Z"
    rows = list(browser.daily_summaries(db, selection))
    assert len(rows) == 3
    later = next(r for r in rows if r["day_utc"] == tomorrow)
    assert later["max_observed_gap_s"] == 120
    assert later["recorded_available_pct"] == 0
    assert later["p95_ttfb_ms"] is None
    selection["since"] = tomorrow + "T00:00:00Z"
    later, = browser.daily_summaries(db, selection)
    assert later["max_observed_gap_s"] is None  # no preceding selected observation


def test_filters_retention_and_no_database_writes(data, monkeypatch):
    db, day, add, _ = data
    add(latency=123)
    add("DOWN", url="https://other.example/")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    add("DOWN", date=old)
    monkeypatch.setenv("HISTORY_RETENTION_DAYS", "30")
    selection = browser.filters({"start": [old], "target": [URL]})
    before = db.read_bytes()
    connect = browser._connect_read_only

    def read_only(path):
        con = connect(path)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM probe_results")
        return con

    monkeypatch.setattr(browser, "_connect_read_only", read_only)
    row, = browser.daily_summaries(db, selection)
    assert row["day_utc"] == day and row["observations"] == 1
    selection["target"] = "' OR 1=1--"
    assert list(browser.daily_summaries(db, selection)) == []
    assert db.read_bytes() == before


def test_single_sample_and_missing_days_do_not_invent_coverage(data):
    db, day, add, selection = data
    add()
    row, = browser.daily_summaries(db, selection)
    assert row["max_observed_gap_s"] is None
    assert row["ttfb_samples"] == 0
    selection["since"] = day + "T12:00:00Z"
    assert list(browser.daily_summaries(db, selection)) == []


def test_summary_pagination_and_export_all_groups(data):
    db, _, add, selection = data
    for i in range(105):
        add(url=f"https://service-{i:03}.example/", latency=i)
    assert len(list(browser.daily_summaries(db, selection))) == 100
    selection["view"] = "daily"
    first = browser.render(db, selection)
    assert "Next</a>" in first and "view=daily" in first
    assert "service-104.example" not in first
    selection["page"] = 2
    assert len(list(browser.daily_summaries(db, selection))) == 5
    second = browser.render(db, selection)
    assert "Previous</a>" in second and "Next</a>" not in second
    assert "service-104.example" in second and "service-000.example" not in second
    raw = b"".join(browser.daily_csv_chunks(db, selection))
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert len(rows) == 105 and raw.startswith(b"\xef\xbb\xbf")
    assert "max_observed_gap_s" in rows[0]


def test_summary_export_protects_formula_cells_and_keeps_missing_values(data):
    db, _, add, selection = data
    add(url='=HYPERLINK("evil")')
    raw = b"".join(browser.daily_csv_chunks(db, selection))
    row, = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    assert row["url"].startswith("'=HYPERLINK")
    assert row["p95_ttfb_ms"] == "" and row["max_observed_gap_s"] == ""


@pytest.fixture
def http(data, tmp_path):
    db, day, add, selection = data
    server = api.serve("127.0.0.1", 0, state_path=tmp_path / "state.json", history_path=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path):
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}" + path, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    yield get, db, day, add, selection
    server.shutdown()
    server.server_close()
    thread.join()


def test_daily_view_has_filters_navigation_and_explicit_limitations(http):
    get, _, day, add, _ = http
    add(latency=42, url="https://example.com/<script>")
    query = urlencode({"start": day, "end": day, "view": "daily", "target": "https://example.com/<script>"})
    code, headers, body = get("/history?" + query)
    text = body.decode()
    assert code == 200 and "Daily summary" in text
    assert "Recorded non-DOWN %" in text and "TTFB samples" in text
    assert "Missing periods" in text and "notification" in text
    assert '<script>' not in text and '&lt;script&gt;' in text
    assert 'name="view" value="daily"' in text
    assert '/history-daily.csv?' in text and "view=samples" in text
    assert "form-action 'self'" in headers["Content-Security-Policy"]


def test_raw_view_keeps_history_and_links_to_daily(http):
    get, _, _, add, _ = http
    add()
    text = get("/history")[2].decode()
    assert "view=daily" in text and "Raw observations" in text and "Download CSV" in text


def test_daily_csv_route_filters_and_download_headers(http):
    get, _, day, add, _ = http
    add(latency=12)
    add("DOWN", url="https://other.example/")
    query = urlencode({"start": day, "end": day, "target": URL, "page": 2})
    code, headers, body = get("/history-daily.csv?" + query)
    assert code == 200 and body.startswith(b"\xef\xbb\xbf")
    assert headers["Content-Disposition"] == 'attachment; filename="vigil-sre-daily-history.csv"'
    assert headers["Cache-Control"] == "no-store"
    assert int(headers["Content-Length"]) == len(body)
    row, = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    assert row["url"] == URL and row["observations"] == "1"


@pytest.mark.parametrize("query", ["view=invalid", "start=oops", "view=daily&view=samples"])
def test_summary_rejects_invalid_filters(http, query):
    get, *_ = http
    assert get("/history?" + query)[0] == 400
    assert get("/history-daily.csv?" + query)[0] == 400


@pytest.mark.parametrize("route", ["/history?view=daily", "/history-daily.csv"])
def test_summary_read_errors_are_errors_before_download_success(http, route):
    get, db, *_ = http
    db.write_bytes(b"corrupt database")
    code, headers, _ = get(route)
    assert code == 500 and "application/json" in headers["Content-Type"]


def test_empty_or_missing_schema_is_honest_and_missing_file_not_created(http):
    get, db, _, _, selection = http
    assert list(browser.daily_summaries(db, selection)) == []
    db.unlink()
    assert list(browser.daily_summaries(db, selection)) == []
    assert get("/history?view=daily")[0] == 200
    assert not db.exists()
    sqlite3.connect(db).close()
    assert list(browser.daily_summaries(db, selection)) == []
