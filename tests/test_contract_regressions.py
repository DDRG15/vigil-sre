"""Boundary cases for configuration, HTML escaping and URL parsing."""
import json
import sqlite3
import threading
import urllib.request
import pytest
import api
import mcp_server
import targetstore


def test_disabled_dotenv_does_not_enable_heartbeat(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED','1')
    monkeypatch.delenv('HEARTBEAT_URL',raising=False)
    env=tmp_path/'test.env';env.write_text('HEARTBEAT_URL=https://example.test/beat\n')
    assert load_dotenv(env,override=False) is False
    server=mcp_server.Server(tmp_path/'state.json',tmp_path/'history.db',env_file=env)
    assert server._get_status({})['alerting']['heartbeat'] is False


@pytest.mark.parametrize('route',['/','/partial/targets'])
def test_state_error_is_html_escaped(tmp_path, monkeypatch, route):
    monkeypatch.setattr(api,'read_state_checked',lambda path: ({},'<script>alert(1)</script>'))
    server=api.serve('127.0.0.1',0,state_path=tmp_path/'state.json',history_path=tmp_path/'history.db')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}{route}') as response:
            body=response.read().decode()
        assert '<script>alert(1)</script>' not in body
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in body
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('tool',['get_history','get_trend'])
def test_unrelated_table_is_not_history(tmp_path, tool):
    db=tmp_path/'history.db'
    with sqlite3.connect(db) as conn: conn.execute('CREATE TABLE unrelated (id INTEGER)')
    server=mcp_server.Server(tmp_path/'state.json',db,env_file=tmp_path/'missing.env')
    result=getattr(server,'_'+tool)({})
    assert result['warnings'][0]['code']=='history_uninitialised'


def test_percent_encoded_credential_name_rejected():
    with pytest.raises(targetstore.ValidationError,match='token'):
        targetstore.validate_entry({'url':'https://example.test/?%74oken=x'})
    assert targetstore.validate_entry({'url':'https://example.test/?page=x'})['url'].endswith('?page=x')
