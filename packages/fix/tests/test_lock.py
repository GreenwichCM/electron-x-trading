import os
import subprocess
import sys

import pytest
from trading_fix import AlreadyRunning, lock_state_dir


def test_second_process_cannot_take_the_lock(tmp_path):
    held = lock_state_dir(tmp_path)
    assert (tmp_path / "lock").read_text() == str(os.getpid())
    code = "import sys, pathlib; from trading_fix import lock_state_dir; lock_state_dir(pathlib.Path(sys.argv[1]))"
    other = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert other.returncode != 0 and "AlreadyRunning" in other.stderr
    held.close()
    again = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert again.returncode == 0  # released once the first holder closes it (or exits)


def test_same_process_twice_is_also_refused(tmp_path):
    held = lock_state_dir(tmp_path)
    with pytest.raises(AlreadyRunning):
        lock_state_dir(tmp_path)
    held.close()
