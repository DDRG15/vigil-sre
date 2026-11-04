"""Raw exports must neither sort nor spool the entire response to disk."""
import asyncio
import csv
import io
import sqlite3
import tempfile
import threading
import urllib.request
from pathlib import Path
import pytest
import api
import history
import history_browser


@pytest.fixture
def db(tmp_path):
    path = tmp_path/'history.db'
    rec = history.HistoryRecorder(path)
    asyncio.run(rec.record_run('2026-01-01T00:00:00Z', [history.CheckOutcome(
        url='https://example.test',status='UP',checked_at='2026-01-01T00:00:00Z')]))
    return path


@pytest.mark.parametrize('target', ['', 'https://example.test'])
def test_export_query_uses_no_temporary_sort(db, monkeypatch, tmp_path, target):
    monkeypatch.setenv('SQLITE_TMPDIR', str(tmp_path/'nonexistent'))
    plans = []
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if 'ORDER BY p.checked_at' in sql:
                plans.extend(row[3] for row in super().execute('EXPLAIN QUERY PLAN '+sql,parameters))
            return super().execute(sql,parameters)
    monkeypatch.setattr(history_browser, '_connect_read_only',
                        lambda p: sqlite3.connect(f'file:{p}?mode=ro',uri=True,factory=Connection))
    rows = list(history_browser.samples(db,dict(since='2025',until='2027',target=target,page=1),paginate=False))
    assert len(rows)==1
    assert plans and not any('TEMP B-TREE' in plan for plan in plans), plans


def test_csv_streams_without_response_spool(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('response attempted disk spooling')
    monkeypatch.setattr(tempfile,'SpooledTemporaryFile',forbidden)
    monkeypatch.setattr(history_browser, 'filters', lambda query: dict(since='2025',until='2027',target='',page=1))
    server=api.serve('127.0.0.1',0,state_path=db.parent/'state.json',history_path=db)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/history.csv') as response:
            assert response.status==200
            assert response.headers['Transfer-Encoding']=='chunked'
            assert response.headers.get('Content-Length') is None
            rows=list(csv.reader(io.StringIO(response.read().decode('utf-8-sig'))))
            assert rows[0]==list(history_browser.COLUMNS)
            assert len(rows)==2 and rows[1][1]=="https://example.test"
    finally:
        server.shutdown();server.server_close();thread.join()


def test_global_export_preserves_time_and_id_order(db):
    rec=history.HistoryRecorder(db)
    asyncio.run(rec.record_run('2026-01-02T00:00:00Z', [
        history.CheckOutcome(url='https://z.test',status='UP',checked_at='2026-01-02T00:00:00Z'),
        history.CheckOutcome(url='https://a.test',status='DOWN',checked_at='2026-01-02T00:00:00Z'),
    ]))
    rows=list(history_browser.samples(db,dict(since='2025',until='2027',target='',page=1),paginate=False))
    assert [row['url'] for row in rows]==['https://a.test','https://z.test','https://example.test']
