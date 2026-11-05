"""Run orderentry: `uv run --env-file services/orderentry/.env python -m trading_orderentry`."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys

import uvicorn
from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_fix import FixSession, SeqStore, build_ssl_context

from .app import OrderEntryApp
from .control import create_control_app
from .risk import Instrument, Throttle
from .settings import ConfigError, Settings
from .sink import RedisOrderSink

log = logging.getLogger("oe")


async def refresh_instruments(redis: Redis, oe: OrderEntryApp, every_s: float = 30.0) -> None:
    """Tick sizes and limits come from marketdata, which keeps instrument:<symbol> current in Redis."""
    while True:
        try:
            keys = [k async for k in redis.scan_iter(match="instrument:*")]
            for value in await redis.mget(keys) if keys else []:
                if value is not None:
                    inst = Instrument.from_dict(json.loads(value))
                    oe.instruments[inst.symbol] = inst
        except (RedisError, OSError) as exc:
            log.warning("could not refresh instruments: %s", exc)
        await asyncio.sleep(every_s)


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    redis = Redis.from_url(settings.redis_url)
    sink = RedisOrderSink(redis)
    oe = OrderEntryApp(
        sink,
        account=settings.account,
        default_trader=settings.default_trader,
        limits=settings.limits,
        throttle=Throttle(settings.rate_limit),
        trader_ids=settings.trader_ids,
        capacity=settings.capacity,
    )
    ssl_context = build_ssl_context(settings.tls) if settings.tls else None
    server_name = settings.tls.server_name if settings.tls else None
    store = SeqStore(settings.state_dir / "seq.json")  # sequence numbers survive restarts
    session = FixSession(settings.session, oe, store, ssl_context, server_name)
    oe.session = session

    control = uvicorn.Server(
        uvicorn.Config(
            create_control_app(oe, settings.internal_token),
            host=settings.control_host,
            port=settings.control_port,
            log_level="warning",
        )
    )
    control.install_signal_handlers = lambda: None  # we handle signals below

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = {
        asyncio.create_task(sink.run(), name="sink"),
        asyncio.create_task(session.run(), name="oe-session"),
        asyncio.create_task(control.serve(), name="control"),
        asyncio.create_task(refresh_instruments(redis, oe), name="instruments"),
    }
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait(tasks | {stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done - {stop_task}:
        if task.exception() is not None:
            log.error("%s failed", task.get_name(), exc_info=task.exception())

    log.info("stopping")
    control.should_exit = True
    await session.stop()
    await sink.close()
    for task in tasks:
        if task.get_name() == "instruments":
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
