"""Order update fan-out: reads the Redis stream `orders` and sends each update only to browsers allowed
to see it (the trader's own orders, or everyone's for risk and management)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from .hub import ClientFeed

log = logging.getLogger("api.orders")

STREAM = "orders"
Order = dict[str, Any]


class OrderHub:
    RETRY_S = 1.0

    def __init__(self, redis: Redis):
        self.redis = redis
        self.clients: dict[ClientFeed, Callable[[Order], bool]] = {}
        self.ready = asyncio.Event()  # set once reading has started

    def connect(self, wants: Callable[[Order], bool]) -> ClientFeed:
        feed = ClientFeed()
        self.clients[feed] = wants
        return feed

    def disconnect(self, feed: ClientFeed) -> None:
        self.clients.pop(feed, None)

    async def run(self) -> None:
        """Read the stream from its current end, then every new entry, resuming after Redis outages.

        The position is kept as a real entry id, not "$": after a reconnect, reading continues exactly
        after the last entry seen, so no update is skipped.
        """
        last_id: bytes | str | None = None
        while True:
            try:
                if last_id is None:
                    newest = await self.redis.xrevrange(STREAM, count=1)
                    last_id = newest[0][0] if newest else "0-0"
                self.ready.set()
                for _stream, entries in await self.redis.xread({STREAM: last_id}, block=1000, count=500) or []:
                    for entry_id, fields in entries:
                        last_id = entry_id
                        order = json.loads(fields[b"data"] if b"data" in fields else fields["data"])
                        for feed, wants in list(self.clients.items()):
                            if wants(order):
                                feed.put(order["cl_ord_id"], order)
            except (RedisError, OSError) as exc:
                self.ready.clear()
                log.warning("reading the orders stream failed (%s); retrying in %.0fs", exc, self.RETRY_S)
                await asyncio.sleep(self.RETRY_S)
