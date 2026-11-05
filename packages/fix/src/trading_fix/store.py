"""Sequence numbers saved to disk, so a restart resumes the session instead of resetting it.

ElectronX keeps Order Entry and Drop Copy sequence numbers for a whole week and resets them
every Sunday at 06:15 UTC. Market Data resets on every logon instead.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("fix.store")


def fix_week_start(now: datetime | None = None) -> datetime:
    """Most recent Sunday 06:15 UTC at or before `now`."""
    now = now or datetime.now(UTC)
    days_since_sunday = (now.weekday() - 6) % 7
    boundary = (now - timedelta(days=days_since_sunday)).replace(hour=6, minute=15, second=0, microsecond=0)
    if boundary > now:
        boundary -= timedelta(days=7)
    return boundary


class SeqStore:
    """Next sequence numbers to send and to expect. With a `path`, every change is saved to that file."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._sender = 1
        self._target = 1
        self._week = fix_week_start().isoformat()
        if path is not None and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("week") == self._week:
                self._sender, self._target = int(data["sender"]), int(data["target"])

    @property
    def next_sender(self) -> int:
        return self._sender

    @property
    def next_target(self) -> int:
        return self._target

    def consume_sender(self) -> int:
        seq = self._sender
        self._sender += 1
        self._save()
        return seq

    def set_sender(self, seq: int) -> None:
        self._sender = seq
        self._save()

    def incr_target(self) -> None:
        self._target += 1
        self._save()

    def set_target(self, seq: int) -> None:
        self._target = seq
        self._save()

    def reset(self) -> None:
        self._sender = self._target = 1
        self._week = fix_week_start().isoformat()
        self._save()

    def roll_week(self) -> bool:
        """Reset if the weekly boundary has passed since the numbers were last used."""
        if self._week != fix_week_start().isoformat():
            self.reset()
            return True
        return False

    def close(self) -> None:
        self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"sender": self._sender, "target": self._target, "week": self._week}), encoding="utf-8"
            )
            os.replace(tmp, self._path)  # atomic: readers see the old file or the new one, never half
        except OSError as exc:
            log.error("could not save sequence numbers to %s: %s", self._path, exc)
