import asyncio

from trading_api import ClientFeed


def test_feed_keeps_only_the_newest_update_per_symbol():
    async def scenario():
        feed = ClientFeed()
        feed.put("X", {"bid": 1})
        feed.put("X", {"bid": 2})
        feed.put("Y", {"bid": 9})
        assert await feed.next_batch() == {"X": {"bid": 2}, "Y": {"bid": 9}}

    asyncio.run(scenario())


def test_snapshot_never_overwrites_a_newer_live_update():
    async def scenario():
        feed = ClientFeed()
        feed.put("X", {"bid": 2})  # live update arrived first
        feed.put("X", {"bid": 1}, replace=False)  # older value read from md:last
        assert await feed.next_batch() == {"X": {"bid": 2}}

    asyncio.run(scenario())
