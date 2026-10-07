"""Exercise restored signal behavior in disposable child processes."""

import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize('previous', ['unknown', 'default', 'ignored', 'callable'])
def test_restored_signal_disposition(previous):
    # A child prevents default/ignored dispositions from affecting pytest or
    # the application running the suite. Verify delivery, not just call args.
    source = Path(__file__).resolve().parents[1] / 'src'
    code = '''
import os
import signal
import sys
from kdiff.deployment.processes import restore_signal_handlers

def previous_handler(signum, frame):
    raise SystemExit(7)

handlers = {'unknown': None, 'default': signal.SIG_DFL,
            'ignored': signal.SIG_IGN, 'callable': previous_handler}
signal.signal(signal.SIGUSR1, lambda *_: None)
restore_signal_handlers({signal.SIGUSR1: handlers[sys.argv[1]]})
os.kill(os.getpid(), signal.SIGUSR1)
'''
    result = subprocess.run([sys.executable, '-c', code, previous],
                            env={**os.environ, 'PYTHONPATH': str(source)},
                            capture_output=True, text=True, timeout=10)
    expected = {'unknown': -signal.SIGUSR1, 'default': -signal.SIGUSR1,
                'ignored': 0, 'callable': 7}
    assert result.returncode == expected[previous], result.stderr
