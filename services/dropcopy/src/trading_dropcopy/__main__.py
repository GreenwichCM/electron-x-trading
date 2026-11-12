"""Run dropcopy: `uv run --env-file services/dropcopy/.env python -m trading_dropcopy`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_core import WrongEnvironment
from trading_core.environment import check_folder, check_redis_async
from trading_fix import AlreadyRunning, FixSession, SeqStore, build_ssl_context, lock_state_dir

from .app import DropCopyApp
from .journal import FillJournal
from .reconcile import Reconciler
from .redis_io import RedisFillSink, follow_orders, trip_kill_switch
from .settings import ConfigError, Settings

log = logging.getLogger("dc")


async def watch(
    redis: Redis, app: DropCopyApp, reconciler: Reconciler, sink: RedisFillSink, every_s: float = 1.0
) -> None:
    """Every second: tell the orders entered on the ElectronX screen from ours and publish them, then
    check our own orders against orderentry (a lasting difference trips the kill switch)."""
    while True:
        await asyncio.sleep(every_s)
        split = reconciler.split(app.orders.values())
        for order in split.screen_orders:
            if order.events and not order.announced:
                log.info("order %s from the ElectronX screen, by %s: %s", order.order_id, order.user, order.symbol)
                order.announced = True
            for event in order.events:  # every report, in order: the order's whole history
                sink.screen_order(event)
            order.events.clear()
        for order in app.orders.values():
            if order.events and reconciler.is_ours(order):
                order.events.clear()  # orderentry records our own orders
        if sink.changed(split):
            sink.positions(app.positions, app.connected)
        if not app.connected:
            continue  # positions may be behind while disconnected; the resend catches them up first
        reason = reconciler.check(split.ours)
        if reason is not None:
            try:
                await trip_kill_switch(redis, reason)
            except (RedisError, OSError) as exc:
                log.error("could not trip the kill switch (%s): %s", exc, reason)


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        lock = lock_state_dir(settings.state_dir)  # noqa: F841 - held until the process exits
    except AlreadyRunning as exc:
        sys.exit(f"dropcopy is already running: {exc}")

    redis = Redis.from_url(settings.redis_url)
    try:
        check_folder(settings.state_dir, settings.environment)
        await check_redis_async(redis, settings.environment)
    except WrongEnvironment as exc:
        await redis.aclose()
        sys.exit(f"wrong environment: {exc}")
    log.info("environment: %s", settings.environment)
    reconciler = Reconciler(grace_s=settings.break_grace_s)
    sink = RedisFillSink(redis)
    app = DropCopyApp(sink, FillJournal(settings.state_dir / "fills.jsonl"))
    sink.classify = lambda: reconciler.split(app.orders.values())
    ssl_context = build_ssl_context(settings.tls) if settings.tls else None
    server_name = settings.tls.server_name if settings.tls else None
    session = FixSession(settings.session, app, SeqStore(settings.state_dir / "seq.json"), ssl_context, server_name)
    sink.positions(app.positions, False)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    sink_task = asyncio.create_task(sink.run(), name="sink")
    session_task = asyncio.create_task(session.run(), name="dc-session")
    helpers = [
        asyncio.create_task(follow_orders(redis, reconciler), name="orders"),
        asyncio.create_task(watch(redis, app, reconciler, sink), name="watch"),
    ]
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait({sink_task, session_task, stop_task, *helpers}, return_when=asyncio.FIRST_COMPLETED)
    for task in done - {stop_task}:
        if task.exception() is not None:
            log.error("%s failed", task.get_name(), exc_info=task.exception())

    log.info("stopping")
    for task in helpers:
        task.cancel()
    await session.stop()
    await session_task
    await sink.close()
    await sink_task
    await asyncio.gather(*helpers, return_exceptions=True)
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
