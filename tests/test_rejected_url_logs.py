"""Rejected target credentials must never be copied into diagnostics."""
import os
import subprocess
import sys
from pathlib import Path
import pytest
import yaml
import main


@pytest.mark.parametrize('object_entry', [False, True])
@pytest.mark.parametrize('url', ['https://example.test/h?token=FAKE_SECRET',
                               'https://user:FAKE_SECRET@example.test/h?token=FAKE_TOKEN'])
def test_rejected_url_redacted(tmp_path, caplog, object_entry, url):
    path = tmp_path / 'targets.yaml'
    path.write_text(yaml.safe_dump({'targets':[{'url':url} if object_entry else url]}))
    with pytest.raises(SystemExit):
        main.load_targets(path, store_path=tmp_path / 'missing.json')
    code = 'import main; from pathlib import Path; main.load_targets(Path(__import__("sys").argv[1]), store_path=Path(__import__("sys").argv[2]))'
    proc = subprocess.run([sys.executable, '-c', code, str(path), str(tmp_path/'missing.json')],
                          cwd=Path(main.__file__).parent, capture_output=True, text=True,
                          env={**os.environ,'LOG_FILE':''})
    assert proc.returncode == 1
    for output in (caplog.text, proc.stdout+proc.stderr):
        assert 'FAKE_SECRET' not in output and 'FAKE_TOKEN' not in output
        assert 'token' in output and 'example.test' in output and 'targets.yaml' in output
