"""Empty history responses explain unreadable state and absent schema."""
import json
import sqlite3
import threading
import urllib.request
import pytest
import api


@pytest.mark.parametrize('route', ['/api/history','/api/status','/history.csv','/history-daily.csv'])
def test_history_responses_explain_empty_data(tmp_path, route):
    state=tmp_path/'state.json';state.write_bytes(b'\xff\xfe')
    db=tmp_path/'history.db'
    with sqlite3.connect(db) as conn: conn.execute('CREATE TABLE unrelated (id INTEGER)')
    server=api.serve('127.0.0.1',0,state_path=state,history_path=db)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}{route}') as response:
            body=response.read()
            if route.endswith('.csv'):
                assert 'history_uninitialised' in response.headers.get('X-Vigil-Warning','')
                assert 'state_unreadable' in response.headers['X-Vigil-Warning']
                assert body.decode('utf-8-sig').count('\n')==1
            else:
                data=json.loads(body)
                assert data['state_error']
                assert {w['code'] for w in data['warnings']}=={'history_uninitialised','state_unreadable'}
    finally:
        server.shutdown();server.server_close();thread.join()
