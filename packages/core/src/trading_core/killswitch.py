"""The desk-wide kill switch, as stored in Redis under KEY. Shared by dropcopy (trips it on a position
break), orderentry (stops trading and cancels everything while it is on) and the api (risk users trip
and reset it). No Redis code here: each service reads and writes the key itself."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

KEY = "killswitch"
ALERTS_STREAM = "alerts"


@dataclass(frozen=True)
class KillSwitchState:
    active: bool = False
    reason: str = ""
    by: str = ""  # a user id, or "dropcopy" when tripped automatically
    time: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes | None) -> KillSwitchState:
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(
            active=bool(data.get("active")),
            reason=str(data.get("reason", "")),
            by=str(data.get("by", "")),
            time=str(data.get("time", "")),
        )
