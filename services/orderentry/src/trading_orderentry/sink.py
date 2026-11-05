"""Where order updates go: the Redis stream `orders` in production, a list in memory for tests.

Every update is kept (no conflation): a stream is a log, so the api and later riskmonitor can
resume from the last entry they read after a restart instead of missing fills.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("oe.sink")

STREAM = "orders"
STREAM_MAXLEN = 100_000  # roughly a few days of updates; Redis trims older entries


class OrderSink(Protocol):
    def publish(self, order: dict[str, Any]) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def publish(self, order: dict[str, Any]) -> None:
        self.updates.append(order)

    def latest(self, cl_ord_id: str) -> dict[str, Any] | None:
        return next((u for u in reversed(self.updates) if u["cl_ord_id"] == cl_ord_id), None)


class RedisOrderSink:
    """XADD each update from a background task, in order, so the FIX session never waits on Redis."""

    RETRY_S = 1.0

    def __init__(self, redis: Redis):
        self.redis = redis
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def publish(self, order: dict[str, Any]) -> None:
        self._queue.put_nowait(order)

    async def run(self) -> None:
        while True:
            order = await self._queue.get()
            if order is None:
                return
            data = json.dumps(order)
            while True:
                try:
                    await self.redis.xadd(STREAM, {"data": data}, maxlen=STREAM_MAXLEN, approximate=True)
                    break
                except (RedisError, OSError) as exc:
                    log.warning("Redis unavailable (%s); retrying in %.0fs", exc, self.RETRY_S)
                    await asyncio.sleep(self.RETRY_S)

    async def close(self) -> None:
        """Send what is queued, then stop run()."""
        self._queue.put_nowait(None)
