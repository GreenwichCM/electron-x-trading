"""Append-only file of fills, one JSON object per line.

Replayed at startup, so positions survive a restart. Together with the saved FIX sequence numbers
this means nothing is counted twice and nothing is missed: fills from before the restart come from the
file, fills during the downtime come from the exchange's resend.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class FillJournal:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def append(self, fill: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fill) + "\n")
            f.flush()
            os.fsync(f.fileno())  # on disk before we act on it: fills are rare and must not be lost
