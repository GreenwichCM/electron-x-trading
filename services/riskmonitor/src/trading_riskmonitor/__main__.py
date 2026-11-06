"""Run riskmonitor:
`uv run --env-file services/db.env --env-file services/riskmonitor/.env python -m trading_riskmonitor`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from redis.asyncio import Redis
from trading_db import ConfigError, follow_rules

from .engine import RiskEngine
from .redis_io import follow_books, follow_streams, publish, refresh_instruments
from .settings import Settings

log = logging.getLogger("risk")


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redis = Redis.from_url(settings.redis_url)
    engine = RiskEngine(max_spread=settings.max_spread)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(follow_streams(redis, engine), name="streams"),
        asyncio.create_task(follow_books(redis, engine), name="books"),
        asyncio.create_task(refresh_instruments(redis, engine), name="instruments"),
        asyncio.create_task(publish(redis, engine, settings.publish_s), name="publish"),
        asyncio.create_task(
            follow_rules(settings.db, engine.set_rules, settings.rules_refresh_s, appname="riskmonitor"), name="rules"
        ),
    ]
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait({*tasks, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done - {stop_task}:
        if task.exception() is not None:
            log.error("%s failed", task.get_name(), exc_info=task.exception())
    log.info("stopping")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
