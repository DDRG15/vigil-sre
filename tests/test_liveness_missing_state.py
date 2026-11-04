"""A stalled first round must eventually fail the liveness probe."""
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
import main


@pytest.mark.parametrize('state_bytes', [None, b'\xff\xfe', b'{', b'{}', b'{"targets":{},"schema_version":2}', b'[]'])
@pytest.mark.parametrize('marker_age,expected', [(5000,1),(0,0),(None,0)])
def test_first_round_liveness(tmp_path, state_bytes, marker_age, expected):
    path = tmp_path/'state.json'
    if state_bytes is not None:
        path.write_bytes(state_bytes)
    marker = tmp_path/'round'
    if marker_age is not None:
        marker.touch(); os.utime(marker, (time.time()-marker_age, time.time()-marker_age))
    env = {**os.environ, 'LOG_FILE':'', 'STATE_FILE_PATH':str(path),
           'LIVENESS_MAX_AGE_S':'900', 'LIVENESS_ROUND_MARKER':str(marker) if marker_age is not None else ''}
    proc = subprocess.run([sys.executable,'-c','import main; main.check_liveness()'],
                          cwd=Path(main.__file__).parent,env=env,capture_output=True,text=True)
    assert proc.returncode == expected, proc.stderr
    if expected: assert 'liveness:' in proc.stderr
