"""History errors stay visible even without any configured targets."""
import json
import sqlite3
import pytest
import mcp_server


@pytest.mark.parametrize('tool',['get_history','get_trend'])
@pytest.mark.parametrize('kind',['corrupt','locked'])
def test_empty_targets_report_unreadable_history(tmp_path, tool, kind):
    db=tmp_path/'history.db'
    conn=None
    if kind=='corrupt':
        db.write_bytes(b'not sqlite'*100)
    else:
        conn=sqlite3.connect(db)
        conn.execute('CREATE TABLE other (id INTEGER)');conn.commit()
        conn.execute('BEGIN EXCLUSIVE')
    try:
        server=mcp_server.Server(tmp_path/'state.json', db, env_file=tmp_path/'missing.env')
        out=json.loads(server.handle_line(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':tool,'arguments':{}}})))['result']
        warning=out['structuredContent']['warnings'][0]
        assert warning['code']=='history_unreadable'
        assert ('locked' if kind=='locked' else 'not a database') in warning['message']
    finally:
        if conn: conn.close()
