"""Trading stops, as stored in Redis.

* The desk-wide kill switch under KEY: dropcopy trips it on a position break, risk users trip and
  reset it, orderentry stops all trading and cancels everything while it is on.
* Per-trader blocks in the hash BLOCKS_KEY (field = trader id): riskmonitor sets one when a trader
  breaches a limit, risk users set and clear them, orderentry stops that trader and cancels their orders.

No Redis code here: each service reads and writes the keys itself.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

KEY = "killswitch"
BLOCKS_KEY = "risk:blocked"
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


@dataclass(frozen=True)
class TraderBlock:
    reason: str
    by: str  # a user id, or "riskmonitor" when set automatically
    time: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> TraderBlock:
        data = json.loads(raw)
        return cls(reason=str(data.get("reason", "")), by=str(data.get("by", "")), time=str(data.get("time", "")))


def decode_blocks(raw: dict) -> dict[str, TraderBlock]:
    """The result of HGETALL BLOCKS_KEY, with bytes or str keys, as trader -> TraderBlock."""
    out: dict[str, TraderBlock] = {}
    for key, value in raw.items():
        trader = key.decode() if isinstance(key, bytes) else key
        out[trader] = TraderBlock.from_json(value)
    return out
