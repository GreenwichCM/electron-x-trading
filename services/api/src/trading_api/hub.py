"""Market data fan-out: one Redis subscription per api process, shared by every connected browser.

The hub also tells marketdata what its browsers show (publish_wanted), so those contracts are subscribed
at the exchange first, and keeps the instrument list in memory for /api/instruments (refresh_instruments).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("api.hub")

Payload = dict[str, Any]
WANTED_KEY = "md:wanted"  # a hash marketdata reads in one call: one field per api process
WANTED_FIELD = f"{socket.gethostname()}-{os.getpid()}"
#: What the price screen needs of each instrument; the rest stays in Redis.
INSTRUMENT_FIELDS = ("symbol", "state", "last_trade_time")


class ClientFeed:
    """Updates waiting to go to one browser. Only the newest per symbol is kept.

    A slow connection (a laptop on hotel wifi) then gets the latest prices when it catches up,
    instead of a growing queue of old ones that would slow the whole process down.

    `watching` is the set of symbols the browser shows; None means all of them. With thousands of
    contracts listed, a browser asks only for those on its screen.
    """

    def __init__(self, watching: set[str] | None = None) -> None:
        self.pending: dict[str, Payload] = {}
        self.watching = watching
        self._wake = asyncio.Event()

    def put(self, symbol: str, payload: Payload, replace: bool = True) -> None:
        if self.watching is not None and symbol not in self.watching:
            return
        if replace or symbol not in self.pending:
            self.pending[symbol] = payload
        self._wake.set()

    def watch(self, symbols: set[str]) -> set[str]:
        """Show only `symbols` from now on. Returns the ones that are new, whose books it still needs."""
        new = symbols - (self.watching or set())
        self.watching = set(symbols)
        self.pending = {s: p for s, p in self.pending.items() if s in symbols}
        return new

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
        self.instrument_list = b""  # JSON of every instrument, INSTRUMENT_FIELDS only
        self.instrument_etag = ""
        self.instrument_count = 0
        self._instruments_loaded = asyncio.Event()

    def connect(self, watching: set[str] | None = None) -> ClientFeed:
        feed = ClientFeed(watching)
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

    def wanted(self) -> set[str]:
        """Every symbol some browser connected here shows."""
        out: set[str] = set()
        for feed in self.clients:
            if feed.watching is not None:
                out |= feed.watching
        return out

    async def publish_wanted(self, every_s: float = 0.5, refresh_s: float = 30.0) -> None:
        """Keep marketdata told what the browsers here show: at once when it changes, and every
        `refresh_s` so the key does not expire while this process runs."""
        last: set[str] | None = None
        written = 0.0
        while True:
            await asyncio.sleep(every_s)
            wanted = self.wanted()
            if wanted == last and time.monotonic() - written < refresh_s:
                continue
            entry = json.dumps({"symbols": sorted(wanted), "time": time.time()})
            try:
                await self.redis.hset(WANTED_KEY, WANTED_FIELD, entry)
                last, written = wanted, time.monotonic()
            except (RedisError, OSError) as exc:
                log.warning("could not tell marketdata what is on screen: %s", exc)

    async def forget_wanted(self) -> None:
        """On shutdown: this process shows nothing any more (marketdata would forget it after 90 s anyway)."""
        try:
            await self.redis.hdel(WANTED_KEY, WANTED_FIELD)
        except RedisError, OSError:
            pass

    async def refresh_instruments(self, every_s: float = 30.0, while_empty_s: float = 2.0) -> None:
        """Reload the instrument list from Redis now and every `every_s` (every `while_empty_s` while there
        is none yet: marketdata may still be starting); browsers get it from memory."""
        while True:
            try:
                await self.load_instruments()
            except (RedisError, OSError) as exc:
                log.warning("could not load the instrument list: %s", exc)
            await asyncio.sleep(every_s if self.instrument_count else while_empty_s)

    async def load_instruments(self) -> None:
        found = await self.instruments()
        rows = [{k: found[s].get(k) for k in INSTRUMENT_FIELDS} for s in sorted(found)]
        data = json.dumps(rows, separators=(",", ":")).encode()
        if data != self.instrument_list:
            self.instrument_list = data
            self.instrument_etag = '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
            self.instrument_count = len(rows)
        self._instruments_loaded.set()

    async def instrument_list_now(self) -> tuple[bytes, str]:
        """The instrument list and its ETag, loading it first if it never has been."""
        if not self._instruments_loaded.is_set():
            await self.load_instruments()
        return self.instrument_list, self.instrument_etag

    async def latest(self) -> dict[str, Payload]:
        """The last book for every symbol, from the md:last:<symbol> keys."""
        return await self._read_prefix("md:last:")

    async def latest_for(self, symbols: set[str]) -> dict[str, Payload]:
        """The last book of each of `symbols` that has one."""
        names = sorted(symbols)
        values = await self.redis.mget([f"md:last:{s}" for s in names]) if names else []
        return {s: json.loads(v) for s, v in zip(names, values, strict=True) if v is not None}

    async def instruments(self) -> dict[str, Payload]:
        return await self._read_prefix("instrument:")

    async def _read_prefix(self, prefix: str) -> dict[str, Payload]:
        keys = [k async for k in self.redis.scan_iter(match=f"{prefix}*", count=1000)]
        if not keys:
            return {}
        values = await self.redis.mget(keys)
        out: dict[str, Payload] = {}
        for key, value in zip(keys, values, strict=True):
            if value is not None:
                name = key.decode() if isinstance(key, bytes) else key
                out[name.removeprefix(prefix)] = json.loads(value)
        return out
