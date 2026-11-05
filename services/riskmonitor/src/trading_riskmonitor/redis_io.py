"""Redis side of riskmonitor.

Reads:
  * streams `orders` (who owns each ClOrdID) and `fills` (the exchange's fills), from the start
  * pub/sub `md.*` plus the keys md:last:<symbol> and instrument:<symbol> (marks and price limits)
Writes, every second:
  * SET risk:snapshot and PUBLISH risk.update   the full snapshot (the api filters it per user)
  * HSET risk:blocked <trader>                  when a trader breaches the loss limit (see trading_core)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_core import ALERTS_STREAM, BLOCKS_KEY, TraderBlock

from .engine import RiskEngine

log = logging.getLogger("risk.redis")

SNAPSHOT_KEY = "risk:snapshot"
UPDATE_CHANNEL = "risk.update"
RETRY_S = 1.0


def _data(fields: dict) -> dict[str, Any]:
    return json.loads(fields[b"data"] if b"data" in fields else fields["data"])


async def follow_streams(redis: Redis, engine: RiskEngine) -> None:
    """Every order and fill since the streams began, then each new one."""
    last = {"orders": "0-0", "fills": "0-0"}
    while True:
        try:
            for stream, entries in await redis.xread(last, block=1000, count=1000) or []:
                name = stream.decode() if isinstance(stream, bytes) else stream
                for entry_id, fields in entries:
                    last[name] = entry_id
                    if name == "orders":
                        engine.on_order(_data(fields))
                    else:
                        engine.on_fill(_data(fields))
        except (RedisError, OSError) as exc:
            log.warning("reading orders/fills failed (%s); retrying", exc)
            await asyncio.sleep(RETRY_S)


async def _load_prefix(redis: Redis, prefix: str) -> list[dict[str, Any]]:
    keys = [k async for k in redis.scan_iter(match=f"{prefix}*")]
    return [json.loads(v) for v in (await redis.mget(keys) if keys else []) if v is not None]


async def follow_books(redis: Redis, engine: RiskEngine) -> None:
    """Latest books and instruments first, then every book update."""
    while True:
        try:
            async with redis.pubsub() as pubsub:
                await pubsub.psubscribe("md.*")
                for inst in await _load_prefix(redis, "instrument:"):
                    engine.on_instrument(inst)
                for book in await _load_prefix(redis, "md:last:"):
                    engine.on_book(book)
                async for msg in pubsub.listen():
                    if msg["type"] == "pmessage":
                        engine.on_book(json.loads(msg["data"]))
        except (RedisError, OSError) as exc:
            log.warning("market data subscription lost (%s); retrying", exc)
            await asyncio.sleep(RETRY_S)


async def refresh_instruments(redis: Redis, engine: RiskEngine, every_s: float = 30.0) -> None:
    while True:
        await asyncio.sleep(every_s)
        try:
            for inst in await _load_prefix(redis, "instrument:"):
                engine.on_instrument(inst)
        except (RedisError, OSError) as exc:
            log.warning("could not refresh instruments: %s", exc)


async def publish(redis: Redis, engine: RiskEngine, every_s: float = 1.0) -> None:
    """Write the snapshot, and block any trader who has breached the loss limit."""
    while True:
        await asyncio.sleep(every_s)
        snapshot = engine.snapshot()
        try:
            data = json.dumps(snapshot)
            await redis.set(SNAPSHOT_KEY, data)
            await redis.publish(UPDATE_CHANNEL, data)
            for trader, reason in engine.breaches(snapshot).items():
                block = TraderBlock(reason=reason, by="riskmonitor", time=datetime.now(UTC).isoformat())
                if await redis.hsetnx(BLOCKS_KEY, trader, block.to_json()):  # only if not already blocked
                    alert = {"type": "trader_blocked", "trader": trader, **json.loads(block.to_json())}
                    await redis.xadd(ALERTS_STREAM, {"data": json.dumps(alert)}, maxlen=10_000)
                    log.critical("BLOCKED %s: %s", trader, reason)
        except (RedisError, OSError) as exc:
            log.warning("could not publish risk (%s)", exc)
