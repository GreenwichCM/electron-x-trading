"""Where book and instrument updates go: Redis in production, a list in memory for tests.

Redis layout:
  * PUBLISH md.<symbol>        every book update (JSON), for live subscribers
  * SET     md:last:<symbol>   the latest book update, so a service that starts later has a value at once
  * SET     instrument:<symbol> reference data (tick size, lot size, limits, last trade and expiration times)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("md.publisher")

Payload = dict[str, Any]


class Publisher(Protocol):
    def book(self, symbol: str, payload: Payload) -> None: ...
    def instrument(self, symbol: str, payload: Payload) -> None: ...


class MemoryPublisher:
    """Keeps everything in memory. Used by tests."""

    def __init__(self) -> None:
        self.books: dict[str, Payload] = {}
        self.book_updates: list[Payload] = []
        self.instruments: dict[str, Payload] = {}

    def book(self, symbol: str, payload: Payload) -> None:
        self.books[symbol] = payload
        self.book_updates.append(payload)

    def instrument(self, symbol: str, payload: Payload) -> None:
        self.instruments[symbol] = payload


class RedisPublisher:
    """Writes to Redis from a background task, so the FIX session never waits on Redis.

    Only the newest pending update per symbol is kept ("conflation"): if Redis is slow or briefly down,
    a busy symbol sends its latest state once instead of building a backlog of stale ones.
    """

    RETRY_S = 1.0

    def __init__(self, redis: Redis):
        self.redis = redis
        self._books: dict[str, Payload] = {}
        self._instruments: dict[str, Payload] = {}
        self._wake = asyncio.Event()
        self._closed = False

    def book(self, symbol: str, payload: Payload) -> None:
        self._books[symbol] = payload
        self._wake.set()

    def instrument(self, symbol: str, payload: Payload) -> None:
        self._instruments[symbol] = payload
        self._wake.set()

    async def run(self) -> None:
        """Publish until close(); call as a task."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._flush()
            if self._closed and not self._books and not self._instruments:
                return

    async def close(self) -> None:
        """Ask run() to send what is pending and return."""
        self._closed = True
        self._wake.set()

    async def _flush(self) -> None:
        books, self._books = self._books, {}
        instruments, self._instruments = self._instruments, {}
        if not books and not instruments:
            return
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for symbol, payload in instruments.items():
                    pipe.set(f"instrument:{symbol}", json.dumps(payload))
                for symbol, payload in books.items():
                    data = json.dumps(payload)
                    pipe.set(f"md:last:{symbol}", data)
                    pipe.publish(f"md.{symbol}", data)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            if self._closed:
                log.error("dropping %d updates at shutdown, Redis unavailable: %s", len(books) + len(instruments), exc)
                return
            log.warning("Redis unavailable (%s); retrying in %.0fs", exc, self.RETRY_S)
            for symbol, payload in books.items():  # keep anything newer that arrived meanwhile
                self._books.setdefault(symbol, payload)
            for symbol, payload in instruments.items():
                self._instruments.setdefault(symbol, payload)
            await asyncio.sleep(self.RETRY_S)
            self._wake.set()
