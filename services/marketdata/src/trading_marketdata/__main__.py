"""Run the market data service: `uv run --env-file services/marketdata/.env python -m trading_marketdata`."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_fix import FixSession, SeqStore, build_ssl_context

from .app import MarketDataApp
from .publisher import RedisPublisher
from .settings import ConfigError, Settings
from .snapshot import SnapshotClient, poll_snapshots

log = logging.getLogger("md")

WANTED_KEY = "md:wanted"  # a hash, one field per api process: {"symbols": [...], "time": seconds}
WANTED_MAX_AGE_S = 90  # an api process that stopped without saying so is forgotten after this
POSITIONS_KEY = "dc:positions"  # dropcopy: the account's positions at the exchange


async def refresh_listing(session: FixSession, app: MarketDataApp, every_s: float) -> None:
    """Ask for the instrument list again now and then: the exchange keeps listing new hourly contracts."""
    while True:
        await asyncio.sleep(every_s)
        if session.logged_on:
            app.request_list(session)


async def follow_interest(redis: Redis, app: MarketDataApp, every_s: float = 1.0) -> None:
    """Contracts on someone's screen and contracts the account holds, for the order of subscriptions."""
    while True:
        try:
            on_screen: set[str] = set()
            for value in (await redis.hgetall(WANTED_KEY)).values():
                entry = json.loads(value)
                if time.time() - float(entry.get("time", 0)) < WANTED_MAX_AGE_S:
                    on_screen.update(entry.get("symbols", ()))
            raw = await redis.get(POSITIONS_KEY)
            held = {s for s, qty in (json.loads(raw) if raw else {}).get("positions", {}).items() if qty}
            app.set_interest(on_screen, held)
        except (RedisError, OSError, ValueError) as exc:
            log.warning("could not read which contracts are wanted: %s", exc)
        await asyncio.sleep(every_s)


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    redis = Redis.from_url(settings.redis_url)
    publisher = RedisPublisher(redis)
    app = MarketDataApp(
        publisher,
        settings.symbols,
        keep_unsettled_s=settings.keep_unsettled_s,
        subscribe_per_s=settings.subscribe_per_s,
        background=settings.background,
    )
    ssl_context = build_ssl_context(settings.tls) if settings.tls else None
    server_name = settings.tls.server_name if settings.tls else None
    session = FixSession(settings.session, app, SeqStore(), ssl_context, server_name)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    publisher_task = asyncio.create_task(publisher.run(), name="publisher")
    session_task = asyncio.create_task(session.run(), name="md-session")
    helpers = [
        asyncio.create_task(refresh_listing(session, app, settings.list_refresh_s), name="listing"),
        asyncio.create_task(follow_interest(redis, app), name="interest"),
    ]
    if settings.snapshot is not None:
        log.info("Snapshot API %s, every %.0fs", settings.snapshot.url, settings.snapshot.poll_s)
        client = SnapshotClient(settings.snapshot.url, settings.snapshot.api_key)
        helpers.append(asyncio.create_task(poll_snapshots(client, app, settings.snapshot.poll_s), name="snapshot"))
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait({publisher_task, session_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done - {stop_task}:
        if task.exception() is not None:
            log.error("%s failed", task.get_name(), exc_info=task.exception())

    log.info("stopping")
    for task in helpers:
        task.cancel()
    await session.stop()
    await session_task
    await publisher.close()
    await publisher_task
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
