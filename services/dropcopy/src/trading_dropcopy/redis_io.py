"""Redis side of dropcopy: publish fills and positions, read orderentry's orders, trip the kill switch.

Redis layout:
  * XADD fills            every fill from the drop copy (a stream: riskmonitor will read it)
  * SET  dc:positions     {"positions": {symbol: lots}, "connected": bool, "time": ...}
  * SET  killswitch       set by trip_kill_switch on a break (see trading_core.killswitch)
  * XADD alerts           a record of each automatic trip
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_core import ALERTS_STREAM, KILLSWITCH_KEY, KillSwitchState

from .reconcile import Reconciler

log = logging.getLogger("dc.redis")

FILLS_STREAM = "fills"
POSITIONS_KEY = "dc:positions"
ORDERS_STREAM = "orders"
STREAM_MAXLEN = 100_000


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RedisFillSink:
    """Writes from a background task, in order, so the FIX session never waits on Redis."""

    RETRY_S = 1.0

    def __init__(self, redis: Redis):
        self.redis = redis
        self._queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    def fill(self, fill: dict[str, Any]) -> None:
        self._queue.put_nowait(("fill", fill))

    def positions(self, positions: dict[str, int], connected: bool) -> None:
        self._queue.put_nowait(("positions", {"positions": dict(positions), "connected": connected, "time": now_iso()}))

    async def run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            kind, data = item
            while True:
                try:
                    if kind == "fill":
                        await self.redis.xadd(FILLS_STREAM, {"data": json.dumps(data)}, maxlen=STREAM_MAXLEN)
                    else:
                        await self.redis.set(POSITIONS_KEY, json.dumps(data))
                    break
                except (RedisError, OSError) as exc:
                    log.warning("Redis unavailable (%s); retrying in %.0fs", exc, self.RETRY_S)
                    await asyncio.sleep(self.RETRY_S)

    async def close(self) -> None:
        self._queue.put_nowait(None)


async def follow_orders(redis: Redis, reconciler: Reconciler) -> None:
    """Feed every orderentry update, from the start of the stream, into the reconciler."""
    last_id: bytes | str = "0-0"
    while True:
        try:
            for _stream, entries in await redis.xread({ORDERS_STREAM: last_id}, block=1000, count=1000) or []:
                for entry_id, fields in entries:
                    last_id = entry_id
                    reconciler.on_order(json.loads(fields[b"data"] if b"data" in fields else fields["data"]))
        except (RedisError, OSError) as exc:
            log.warning("reading the orders stream failed (%s); retrying", exc)
            await asyncio.sleep(1.0)


async def trip_kill_switch(redis: Redis, reason: str) -> None:
    current = KillSwitchState.from_json(await redis.get(KILLSWITCH_KEY))
    if current.active:
        return  # already on; keep the original reason
    state = KillSwitchState(active=True, reason=reason, by="dropcopy", time=now_iso())
    await redis.set(KILLSWITCH_KEY, state.to_json())
    await redis.xadd(ALERTS_STREAM, {"data": state.to_json()}, maxlen=10_000)
    log.critical("KILL SWITCH TRIPPED: %s", reason)
