from __future__ import annotations
import asyncio
import csv
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import api
import dashboard
import main
from diagnostics import ProbePhases
from history import CheckOutcome, HistoryRecorder

URL = 'https://service.example/'

@pytest.fixture
def browser(tmp_path):
    db = tmp_path / 'history.db'
    recorder = HistoryRecorder(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    asyncio.run(recorder.record_run(stamp, [CheckOutcome(url=URL,status='UP',checked_at=stamp,
        phases=ProbePhases(url=URL,http_status=200,transfer_ms=1.0,body_bytes=10,ttfb_ms=123.5,dns_ms=12.0))]))
    server = api.serve('127.0.0.1',0,state_path=tmp_path/'state.json',history_path=db)
    t = threading.Thread(target=server.serve_forever,daemon=True)
    t.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    def get(path):
        try:
            with urllib.request.urlopen(base+path,timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code,e.headers,e.read()
    yield get,db,now
    server.shutdown(); server.server_close(); t.join()

def test_history_page_shows_raw_observation_and_download(browser):
    get,_,_ = browser
    code,headers,body = get('/history')
    s=body.decode()
    assert code == 200
    assert URL in s and '123.5' in s and 'UTC' in s
    assert 'Download CSV' in s and '1 observation' in s
    assert "form-action 'self'" in headers['Content-Security-Policy']

def test_csv_has_excel_encoding_units_and_original_sample(browser):
    get,_,_ = browser
    code,h,b = get('/history.csv')
    assert code == 200 and b.startswith(b'\xef\xbb\xbf')
    assert 'attachment;' in h['Content-Disposition']
    rows=list(csv.DictReader(io.StringIO(b.decode('utf-8-sig'))))
    assert len(rows)==1 and rows[0]['url']==URL
    assert rows[0]['ttfb_ms']=='123.5' and rows[0]['tls_ms']==''

@pytest.mark.parametrize('query',['start=oops','start=2026-01-02&end=2026-01-01','page=0','page=xyz','end=2999-01-01','start=2026-01-01&start=2026-01-02'])
def test_history_rejects_invalid_filters(browser,query):
    get,_,_=browser
    assert get('/history?'+query)[0] == 400

def test_history_filter_and_empty_database_are_honest(browser):
    get,db,_=browser
    assert '<p role="status">0 observations' in get('/history?target=https%3A%2F%2Fother.example')[2].decode()
    db.unlink()
    code,_,b=get('/history')
    assert code==200 and 'No observations' in b.decode()
    assert not db.exists()

def test_csv_escapes_formulas_and_html_page_escapes_errors(browser):
    get,db,_=browser
    with sqlite3.connect(db) as c:
        c.execute('UPDATE probe_results SET error=?,status=?',('=HYPERLINK("evil")<script>','DOWN'))
    text=get('/history')[2].decode()
    assert '<script>' not in text and '&lt;script&gt;' in text
    rows=list(csv.DictReader(io.StringIO(get('/history.csv')[2].decode('utf-8-sig'))))
    assert rows[0]['error'].startswith("'=HYPERLINK")

def test_corrupt_history_returns_error_not_download_success(browser):
    get,db,_=browser
    db.write_bytes(b'not sqlite')
    assert get('/history')[0]==500
    assert get('/history.csv')[0]==500

def test_dashboard_has_history_and_editor_advanced_group():
    s=dashboard.render_page({}, {}, None)
    assert 'href="/history"' in s
    assert 'Advanced settings' in dashboard._EDITOR_SCRIPT
    assert 'Expected HTTP status' in dashboard._EDITOR_SCRIPT

@pytest.fixture
def probe(monkeypatch):
    sent=[]
    async def dispatch(session,url,status_detail,kind):
        sent.append(kind.name); return True
    async def no_sample(*a,**k): return None
    monkeypatch.setattr(main,'dispatch_alert',dispatch)
    monkeypatch.setattr(main,'sample_connection',no_sample)
    async def observe(state,slow):
        async def run(*a,**k):
            if slow is None: raise main._ProbeFailure('Connection refused')
            return ProbePhases(url=URL,http_status=200,transfer_ms=1.0,body_bytes=10,ttfb_ms=4000 if slow else 20)
        monkeypatch.setattr(main,'_probe_with_backoff',run)
        return await main.check_url(URL,None,state)
    return sent,observe

async def test_isolated_slow_observation_is_stored_without_notification(tmp_path,probe):
    sent,observe=probe
    state=main.StateManager(tmp_path/'state.json')
    assert (await observe(state,True)).status=='DEGRADED'
    await observe(state,False)
    assert sent==[]

async def test_sustained_slowdown_and_stable_recovery_survive_restart(tmp_path,probe):
    sent,observe=probe
    path=tmp_path/'state.json'
    for _ in range(3):
        await observe(main.StateManager(path),True)
    assert sent==['DEGRADED']
    await observe(main.StateManager(path),False)
    assert sent==['DEGRADED']
    await observe(main.StateManager(path),True)
    assert sent==['DEGRADED']
    await observe(main.StateManager(path),False)
    await observe(main.StateManager(path),False)
    assert sent==['DEGRADED','PERFORMANCE_RECOVERY']

async def test_down_escalation_bypasses_performance_confirmation(tmp_path,probe):
    sent,observe=probe
    state=main.StateManager(tmp_path/'state.json')
    await observe(state,True)
    await observe(state,None)
    await observe(state,None)
    assert sent==['FAILURE']
    await observe(state,False)
    assert sent==['FAILURE','RECOVERY']

async def test_warning_delivery_retries_without_new_confirmation(tmp_path,monkeypatch,probe):
    sent,observe=probe
    delivered=False
    async def dispatch(session,url,status_detail,kind):
        sent.append(kind.name)
        return delivered
    monkeypatch.setattr(main,'dispatch_alert',dispatch)
    path=tmp_path/'state.json'
    for _ in range(3): await observe(main.StateManager(path),True)
    assert sent==['DEGRADED']
    assert json.loads(path.read_text())['targets'][URL]['alert_pending']
    delivered=True
    await observe(main.StateManager(path),True)
    assert sent==['DEGRADED','DEGRADED']
    assert not json.loads(path.read_text())['targets'][URL]['alert_pending']

async def test_unannounced_warning_does_not_generate_closure(tmp_path,monkeypatch,probe):
    sent,observe=probe
    async def dispatch(session,url,status_detail,kind):
        sent.append(kind.name); return False
    monkeypatch.setattr(main,'dispatch_alert',dispatch)
    state=main.StateManager(tmp_path/'state.json')
    for _ in range(3): await observe(state,True)
    await observe(state,False)
    await observe(state,False)
    assert sent==['DEGRADED']
    assert not state._state[URL].get('alert_pending')

async def test_missing_sampling_period_breaks_confirmation(tmp_path,probe):
    sent,observe=probe
    path=tmp_path/'state.json'
    state=main.StateManager(path)
    await observe(state,True); await observe(state,True)
    data=json.loads(path.read_text())
    data['targets'][URL]['last_checked']='2000-01-01T00:00:00Z'
    path.write_text(json.dumps(data))
    state=main.StateManager(path)
    await observe(state,True)
    assert sent==[]
    await observe(state,True); await observe(state,True)
    assert sent==['DEGRADED']

async def test_recovery_retry_is_not_lost_on_restart_or_new_slow_observation(tmp_path,monkeypatch,probe):
    sent,observe=probe
    path=tmp_path/'state.json'
    for _ in range(3): await observe(main.StateManager(path),True)
    async def fail(session,url,status_detail,kind):
        sent.append(kind.name); return False
    monkeypatch.setattr(main,'dispatch_alert',fail)
    for _ in range(2): await observe(main.StateManager(path),False)
    assert sent==['DEGRADED','PERFORMANCE_RECOVERY']
    await observe(main.StateManager(path),True)
    assert sent==['DEGRADED','PERFORMANCE_RECOVERY']
    assert not main.StateManager(path)._state[URL]['alert_pending']

async def test_down_alert_failed_before_recovery_announces_a_missed_outage_not_a_recovery(tmp_path,monkeypatch,probe):
    sent,observe=probe
    async def fail(session,url,status_detail,kind):
        sent.append(kind.name); return False
    monkeypatch.setattr(main,'dispatch_alert',fail)
    state=main.StateManager(tmp_path/'state.json')
    await observe(state,False)
    await observe(state,None); await observe(state,None)
    await observe(state,False)
    # No unmatched RECOVERY; the lost outage is owed one catch-up notice instead.
    assert 'RECOVERY' not in sent
    assert sent==['FAILURE','UNANNOUNCED_OUTAGE']

def test_csv_exports_all_pages_and_selection_is_parameterized(browser):
    get,db,now=browser
    stamp=now.strftime('%Y-%m-%dT%H:%M:%SZ')
    with sqlite3.connect(db) as c:
        c.executemany('INSERT INTO probe_results(run_started_at,url,checked_at,status) VALUES(?,?,?,?)',[(stamp,URL,stamp,'DOWN')]*105)
    text=get('/history')[2].decode()
    assert '106 observations' in text
    assert 'Next</a>' in text
    text2=get('/history?page=2')[2].decode()
    assert 'Previous</a>' in text2
    rows=list(csv.DictReader(io.StringIO(get('/history.csv')[2].decode('utf-8-sig'))))
    assert len(rows)==106
    assert '<p role="status">0 observations' in get('/history?target=%27%20OR%201%3D1--')[2].decode()

def test_retention_is_enforced_even_without_writer_pruning(browser):
    get,db,now=browser
    old=(now-timedelta(days=40)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO probe_results(run_started_at,url,checked_at,status) VALUES(?,?,?,?)',(old,URL,old,'DOWN'))
    rows=list(csv.DictReader(io.StringIO(get('/history.csv?start='+old[:10])[2].decode('utf-8-sig'))))
    assert len(rows)==1
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT count(*) FROM probe_results').fetchone()[0]==2

def test_date_range_is_utc_and_end_date_is_inclusive(browser):
    get,db,now=browser
    old=(now-timedelta(days=2)).date().isoformat()
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO probe_results(run_started_at,url,checked_at,status) VALUES(?,?,?,?)',(old+'T23:59:59Z',URL,old+'T23:59:59Z','DOWN'))
    rows=list(csv.DictReader(io.StringIO(get('/history.csv?start='+old+'&end='+old)[2].decode('utf-8-sig'))))
    assert len(rows)==1 and rows[0]['checked_at']==old+'T23:59:59Z'

def test_compact_notification_keeps_target_and_one_timestamp():
    from notifiers import AlertKind, DiscordNotifier
    e=DiscordNotifier().build_payload(URL,'SLOW_DNS (dns_ms=550 (threshold 200))','2026-10-17T14:00:00Z',AlertKind.DEGRADED)['embeds'][0]
    assert len(e['fields'])==2
    assert e['timestamp']=='2026-10-17T14:00:00Z'
    assert 'Slow DNS' in json.dumps(e)
    assert '550' in json.dumps(e)
    assert 'threshold 200' not in json.dumps(e)

def test_default_download_includes_entire_rolling_retention_window(browser):
    get,db,now=browser
    old=(now-timedelta(days=29,hours=23)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO probe_results(run_started_at,url,checked_at,status) VALUES(?,?,?,?)',(old,URL,old,'DOWN'))
    rows=list(csv.DictReader(io.StringIO(get('/history.csv')[2].decode('utf-8-sig'))))
    assert len(rows)==2

def test_dashboard_tolerates_an_invalid_stored_host_name():
    text=dashboard.render_rows({'http://[broken':{'status':'UP'}},{},0)
    assert 'http://[broken' in text
