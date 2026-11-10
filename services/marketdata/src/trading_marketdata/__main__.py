"""Run the market data service: `uv run --env-file services/marketdata/.env python -m trading_marketdata`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from redis.asyncio import Redis
from trading_fix import FixSession, SeqStore, build_ssl_context

from .app import MarketDataApp
from .publisher import RedisPublisher
from .settings import ConfigError, Settings

log = logging.getLogger("md")


async def refresh_listing(session: FixSession, app: MarketDataApp, every_s: float) -> None:
    """Ask for the instrument list again now and then: the exchange keeps listing new hourly contracts."""
    while True:
        await asyncio.sleep(every_s)
        if session.logged_on:
            app.request_list(session)


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    redis = Redis.from_url(settings.redis_url)
    publisher = RedisPublisher(redis)
    app = MarketDataApp(publisher, settings.symbols, keep_unsettled_s=settings.keep_unsettled_s)
    ssl_context = build_ssl_context(settings.tls) if settings.tls else None
    server_name = settings.tls.server_name if settings.tls else None
    session = FixSession(settings.session, app, SeqStore(), ssl_context, server_name)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    publisher_task = asyncio.create_task(publisher.run(), name="publisher")
    session_task = asyncio.create_task(session.run(), name="md-session")
    refresh_task = asyncio.create_task(refresh_listing(session, app, settings.list_refresh_s), name="listing")
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait({publisher_task, session_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done - {stop_task}:
        if task.exception() is not None:
            log.error("%s failed", task.get_name(), exc_info=task.exception())

    log.info("stopping")
    refresh_task.cancel()
    await session.stop()
    await session_task
    await publisher.close()
    await publisher_task
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
