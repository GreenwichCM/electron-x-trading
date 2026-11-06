"""Run orderentry: `uv run --env-file services/orderentry/.env python -m trading_orderentry`."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

import uvicorn
from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_core import BLOCKS_KEY, KILLSWITCH_KEY, KillSwitchState, decode_blocks
from trading_fix import AlreadyRunning, FixSession, SeqStore, build_ssl_context, lock_state_dir

from .app import OrderEntryApp
from .control import create_control_app
from .recovery import Recovered, recover, save_state
from .risk import Instrument, Throttle
from .settings import ConfigError, Settings
from .sink import RedisOrderSink

log = logging.getLogger("oe")

SAVE_EVERY_S = 1.0
SAVE_AT_LEAST_EVERY_S = 60.0  # even with no changes, so the file shows when orderentry last ran


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


async def watch_kill_switch(redis: Redis, oe: OrderEntryApp, every_s: float = 0.5) -> None:
    """Follow the kill switch and the trader blocks in Redis. If Redis is unreachable the last known
    state is kept."""
    while True:
        try:
            oe.set_kill_switch(KillSwitchState.from_json(await redis.get(KILLSWITCH_KEY)))
            oe.set_blocks(decode_blocks(await redis.hgetall(BLOCKS_KEY)))
        except (RedisError, OSError) as exc:
            log.warning("could not read the kill switch or trader blocks: %s", exc)
        oe.cancel_unconfirmed()
        await asyncio.sleep(every_s)


async def recover_when_possible(redis: Redis, path: Path) -> Recovered:
    """Orders and positions of the previous run. Waits for Redis: without the stream's tail, positions
    could be wrong, so orderentry does not connect to the exchange until this has worked."""
    while True:
        try:
            return await recover(redis, path)
        except (RedisError, OSError) as exc:
            log.warning("cannot read the orders stream to recover (%s); retrying", exc)
            await asyncio.sleep(1.0)


def save(path: Path, oe: OrderEntryApp, sink: RedisOrderSink, rec: Recovered) -> None:
    save_state(path, oe.orders.values(), oe.positions, sink.last_id or rec.stream_id)


async def save_regularly(path: Path, oe: OrderEntryApp, sink: RedisOrderSink, rec: Recovered) -> None:
    saved_changes, saved_at = -1, 0.0
    while True:
        await asyncio.sleep(SAVE_EVERY_S)
        if oe.changes != saved_changes or time.monotonic() - saved_at > SAVE_AT_LEAST_EVERY_S:
            saved_changes, saved_at = oe.changes, time.monotonic()
            save(path, oe, sink, rec)


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        lock = lock_state_dir(settings.state_dir)  # noqa: F841 - held until the process exits
    except AlreadyRunning as exc:
        sys.exit(f"orderentry is already running: {exc}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stop_task = asyncio.create_task(stop.wait(), name="stop")

    redis = Redis.from_url(settings.redis_url)
    state_path = settings.state_dir / "orders.json"
    recovering = asyncio.create_task(recover_when_possible(redis, state_path), name="recover")
    await asyncio.wait({recovering, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if not recovering.done():
        recovering.cancel()
        await redis.aclose()
        return
    rec = recovering.result()

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
    oe.restore(rec, from_earlier_week=rec.from_earlier_week())
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
    control.install_signal_handlers = lambda: None  # we handle signals above

    tasks = {
        asyncio.create_task(sink.run(), name="sink"),
        asyncio.create_task(session.run(), name="oe-session"),
        asyncio.create_task(control.serve(), name="control"),
        asyncio.create_task(refresh_instruments(redis, oe), name="instruments"),
        asyncio.create_task(watch_kill_switch(redis, oe), name="killswitch"),
        asyncio.create_task(save_regularly(state_path, oe, sink, rec), name="save"),
    }
    done, _ = await asyncio.wait(tasks | {stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done - {stop_task}:
        if task.exception() is not None:
            log.error("%s failed", task.get_name(), exc_info=task.exception())

    log.info("stopping")
    control.should_exit = True
    await session.stop()
    await sink.close()
    for task in tasks:
        if task.get_name() in ("instruments", "killswitch", "save"):
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    save(state_path, oe, sink, rec)  # after the sink has written everything, so nothing is replayed twice
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
