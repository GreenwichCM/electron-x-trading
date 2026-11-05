"""Market data fan-out: one Redis subscription per api process, shared by every connected browser."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("api.hub")

Payload = dict[str, Any]


class ClientFeed:
    """Updates waiting to go to one browser. Only the newest per symbol is kept.

    A slow connection (a laptop on hotel wifi) then gets the latest prices when it catches up,
    instead of a growing queue of old ones that would slow the whole process down.
    """

    def __init__(self) -> None:
        self.pending: dict[str, Payload] = {}
        self._wake = asyncio.Event()

    def put(self, symbol: str, payload: Payload, replace: bool = True) -> None:
        if replace or symbol not in self.pending:
            self.pending[symbol] = payload
        self._wake.set()

    async def next_batch(self) -> dict[str, Payload]:
        await self._wake.wait()
        self._wake.clear()
        batch, self.pending = self.pending, {}
        return batch


class MarketDataHub:
    RETRY_S = 1.0

    def __init__(self, redis: Redis):
        self.redis = redis
        self.clients: set[ClientFeed] = set()
        self.ready = asyncio.Event()  # set once the Redis subscription is active

    def connect(self) -> ClientFeed:
        feed = ClientFeed()
        self.clients.add(feed)
        return feed

    def disconnect(self, feed: ClientFeed) -> None:
        self.clients.discard(feed)

    async def run(self) -> None:
        """Forward every md.* message to every client; reconnects if Redis goes away."""
        while True:
            try:
                async with self.redis.pubsub() as pubsub:
                    await pubsub.psubscribe("md.*")
                    self.ready.set()
                    log.info("subscribed to md.*")
                    async for msg in pubsub.listen():
                        if msg["type"] != "pmessage":
                            continue
                        payload = json.loads(msg["data"])
                        for feed in self.clients:
                            feed.put(payload["symbol"], payload)
            except (RedisError, OSError) as exc:
                self.ready.clear()
                log.warning("Redis subscription lost (%s); retrying in %.0fs", exc, self.RETRY_S)
                await asyncio.sleep(self.RETRY_S)

    async def latest(self) -> dict[str, Payload]:
        """The last book for every symbol, from the md:last:<symbol> keys."""
        return await self._read_prefix("md:last:")

    async def instruments(self) -> dict[str, Payload]:
        return await self._read_prefix("instrument:")

    async def _read_prefix(self, prefix: str) -> dict[str, Payload]:
        keys = [k async for k in self.redis.scan_iter(match=f"{prefix}*")]
        if not keys:
            return {}
        values = await self.redis.mget(keys)
        out: dict[str, Payload] = {}
        for key, value in zip(keys, values, strict=True):
            if value is not None:
                name = key.decode() if isinstance(key, bytes) else key
                out[name.removeprefix(prefix)] = json.loads(value)
        return out
