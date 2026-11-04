"""Invalid text must remain observable and recover on the next round."""
import json
import threading
import urllib.request

import pytest
import api
import main
import mcp_server


@pytest.fixture
def bad_state(tmp_path):
    path = tmp_path / 'state.json'
    path.write_bytes(b'\xff\xfe')
    return path


@pytest.mark.asyncio
async def test_round_recovers_invalid_text(bad_state, monkeypatch, caplog):
    probed = []
    async def check(url, session, state, **kwargs):
        probed.append(url)
        await state.set_up(url)
        return main.CheckOutcome(url=url, status='UP', checked_at='2026-01-01T00:00:00Z', phases=None)
    monkeypatch.setattr(main, 'check_url', check)
    await main.run_health_checks(targets=[main.Target(url='https://example.test')],
                                 state_path=bad_state, history_path=bad_state.parent / 'history.db')
    assert probed == ['https://example.test']
    assert json.loads(bad_state.read_text())['targets'][probed[0]]['status'] == 'UP'
    assert 'starting with empty state' in caplog.text


def test_api_reports_invalid_text(bad_state):
    targets, error = api.read_state_checked(bad_state)
    assert targets == {} and 'UnicodeDecodeError' in error


@pytest.mark.parametrize('route', ['/api/status', '/', '/partial/targets'])
def test_http_reports_invalid_text(bad_state, route):
    server = api.serve('127.0.0.1', 0, state_path=bad_state, history_path=bad_state.parent / 'history.db')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}{route}') as response:
            assert response.status == 200
            assert 'UnicodeDecodeError' in response.read().decode()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_mcp_reports_invalid_text(bad_state):
    server = mcp_server.Server(bad_state, bad_state.parent / 'history.db', env_file=bad_state.parent / 'missing.env')
    result = json.loads(server.handle_line(json.dumps({'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
        'params':{'name':'get_status','arguments':{}}})))['result']
    assert result['isError'] is False
    assert result['structuredContent']['warnings'][0]['code'] == 'state_unreadable'


def test_pending_alerts_tolerates_invalid_text(bad_state):
    assert main.undelivered_alerts(bad_state) == 0
