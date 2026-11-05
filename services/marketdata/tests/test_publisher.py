"""RedisPublisher against fakeredis, an in-memory Redis that needs no server."""

import asyncio
import json

from fakeredis import FakeAsyncRedis, FakeServer
from trading_marketdata import RedisPublisher


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 10))


def test_latest_value_is_stored_and_published_once():
    async def scenario():
        r = FakeAsyncRedis()
        pubsub = r.pubsub()
        await pubsub.subscribe("md.X")
        await pubsub.get_message(timeout=1)  # subscribe confirmation

        pub = RedisPublisher(r)
        task = asyncio.create_task(pub.run())
        pub.book("X", {"bid": 1.0})
        pub.book("X", {"bid": 2.0})  # arrives before the first is sent: only the newest goes out
        pub.instrument("X", {"tick_size": 0.25})
        await pub.close()
        await task

        assert json.loads(await r.get("md:last:X")) == {"bid": 2.0}
        assert json.loads(await r.get("instrument:X")) == {"tick_size": 0.25}
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        assert json.loads(msg["data"]) == {"bid": 2.0}
        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) is None
        await pubsub.aclose()

    run(scenario())


def test_updates_wait_while_redis_is_down():
    async def scenario():
        server = FakeServer()
        server.connected = False
        r = FakeAsyncRedis(server=server)
        pub = RedisPublisher(r)
        pub.RETRY_S = 0.1
        task = asyncio.create_task(pub.run())
        pub.book("X", {"bid": 1.0})
        await asyncio.sleep(0.3)  # a few failed attempts
        server.connected = True
        for _ in range(50):
            if await r.get("md:last:X") is not None:
                break
            await asyncio.sleep(0.05)
        assert json.loads(await r.get("md:last:X")) == {"bid": 1.0}
        await pub.close()
        await task

    run(scenario())
