"""One process per FIX session: a lock on the session's state folder.

Two copies of orderentry or dropcopy would share one SenderCompID (the exchange refuses the second
logon) and one sequence number file (they would overwrite each other's numbers). The lock stops the
second copy before it does either.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO


class AlreadyRunning(Exception):
    pass


def lock_state_dir(state_dir: Path) -> TextIO:
    """Take an exclusive lock on `state_dir`/lock for the life of the process (keep the returned file open).

    The operating system releases the lock when the process exits, even after a crash.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    f = (state_dir / "lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip() or "?"
        f.close()
        raise AlreadyRunning(f"{state_dir} is in use by another process (pid {holder})") from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f
