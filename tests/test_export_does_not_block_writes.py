"""A CSV export must not hold SQLite's read lock while the client downloads.

history.db uses the rollback journal: an open read blocks the monitor's next
commit. The export streams, so a statement kept open across the stream holds
that read for as long as the client takes to read -- a stalled download made
every round's history write fail with "database is locked". The monitor must
be able to commit while a download sits half-read.
"""
from __future__ import annotations

import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import api
import history


def _seed(db, *, targets: int, per_target: int, step_s: int) -> None:
    history.HistoryRecorder(db)
    now = datetime.now(timezone.utc)
    rows = []
    for t in range(targets):
        for i in range(per_target):
            at = (now - timedelta(seconds=step_s * i + t)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append((at, f"https://t{t}.example.invalid/", at, "UP", 200, 100.0, "x" * 80))
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO probe_results(run_started_at,url,checked_at,status,"
            "http_status,ttfb_ms,error) VALUES(?,?,?,?,?,?,?)", rows)


def _commit_while_download_stalls(tmp_path, db, route: str) -> tuple[bytes, float]:
    srv = api.serve(host="127.0.0.1", port=0,
                    state_path=tmp_path / "state.json", history_path=db)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.sendall(f"GET {route} HTTP/1.1\r\nHost: test\r\n\r\n".encode())
        first = client.recv(2048)          # then stop reading: a stalled client
        time.sleep(1.0)
        started = time.monotonic()
        writer = sqlite3.connect(db, timeout=2.0)
        try:
            writer.execute("INSERT INTO probe_results(run_started_at,url,checked_at,"
                           "status) VALUES('r','https://w.example.invalid/','c','UP')")
            writer.commit()
        finally:
            writer.close()
        client.close()
        return first, time.monotonic() - started
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route,targets,per_target,step_s", [
    ("/history.csv", 4, 10000, 60),          # ~6 MB of raw samples
    ("/history-daily.csv", 6000, 6, 86400),  # ~6000 targets x 6 days of summaries
])
def test_a_stalled_csv_download_does_not_lock_out_the_monitor(
        tmp_path, route, targets, per_target, step_s):
    db = tmp_path / "history.db"
    _seed(db, targets=targets, per_target=per_target, step_s=step_s)
    first, waited = _commit_while_download_stalls(tmp_path, db, route)
    assert first.startswith(b"HTTP/1.1 200"), first[:80]   # control: the export started
    assert waited < 1.0, f"the monitor's commit waited {waited:.1f}s on the export"


def test_the_batched_export_returns_every_row_once_newest_first(tmp_path, monkeypatch):
    import history_browser
    monkeypatch.setattr(history_browser, "SAMPLE_BATCH_ROWS", 7)
    db = tmp_path / "history.db"
    history.HistoryRecorder(db)
    # Equal timestamps across batch boundaries: the id tie-breaker must not
    # skip or repeat a row when a batch ends inside a run of equal times.
    rows = [("r", f"https://t{i % 3}.example.invalid/", "2026-11-01T00:00:%02dZ" % (i // 9), "UP")
            for i in range(100)]
    with sqlite3.connect(db) as c:
        c.executemany("INSERT INTO probe_results(run_started_at,url,checked_at,status) "
                      "VALUES(?,?,?,?)", rows)
    f = {"target": None, "since": "2026-10-01T00:00:00Z", "until": "2026-12-01T00:00:00Z", "page": 1}
    got = list(history_browser.samples(db, f, paginate=False))
    assert len(got) == 100
    keys = [(r["checked_at"], r["url"]) for r in got]
    assert keys == sorted(keys, key=lambda k: k[0], reverse=True)
    from collections import Counter
    assert Counter(r["url"] for r in got) == Counter(r[1] for r in rows)


def test_daily_summaries_order_days_newest_first_and_urls_ascending(tmp_path):
    import history_browser
    db = tmp_path / "history.db"
    history.HistoryRecorder(db)
    today = datetime.now(timezone.utc).date()
    rows = []
    # Fewest observations for the last URL, so an order by count is visibly wrong.
    for days_ago in (1, 2):
        day = (today - timedelta(days=days_ago)).isoformat()
        for url, n in (("https://c.example.invalid/", 1),
                       ("https://a.example.invalid/", 5),
                       ("https://b.example.invalid/", 3)):
            rows += [("r", url, f"{day}T00:00:{i:02d}Z", "UP") for i in range(n)]
    with sqlite3.connect(db) as c:
        c.executemany("INSERT INTO probe_results(run_started_at,url,checked_at,status) "
                      "VALUES(?,?,?,?)", rows)
    f = history_browser.filters({})
    got = [(r["day_utc"], r["url"]) for r in history_browser.daily_summaries(db, f, paginate=False)]
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    assert got == [(d, f"https://{u}.example.invalid/") for d in (d1, d2) for u in "abc"]
