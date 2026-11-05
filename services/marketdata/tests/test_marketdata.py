"""MarketDataApp against the simulated MD gateway, over real TCP on 127.0.0.1."""

import asyncio
import time

from trading_fix import FixSession, SeqStore, SessionConfig
from trading_marketdata import MarketDataApp, MemoryPublisher
from trading_sim import BID, BUY, OFFER, SimGateway, SimInstrument, SimMarketData

SENDER = "TEST-MD01"
INSTRUMENTS = [
    SimInstrument("BFUT500-ERCOT-HB_NORTH-261231-15", fair=45.0),
    SimInstrument("BFUT100-PJM-WESTERN_HUB-261231-15", fair=52.0, high=100.0),
]
SYMBOLS = [i.symbol for i in INSTRUMENTS]


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 20))


class Harness:
    def __init__(self, symbols=()):
        self.symbols = symbols

    async def __aenter__(self):
        self.gw = SimGateway({SENDER: "sim"})
        self.md = SimMarketData(self.gw, INSTRUMENTS, seed=7)
        port = await self.gw.start()
        self.pub = MemoryPublisher()
        self.app = MarketDataApp(self.pub, self.symbols)
        cfg = SessionConfig(
            name="md",
            host="127.0.0.1",
            port=port,
            sender_comp_id=SENDER,
            target_comp_id="EXI",
            password="sim",
            reset_on_logon=True,
        )
        self.session = FixSession(cfg, self.app, SeqStore())
        self.task = asyncio.create_task(self.session.run())
        return self

    async def __aexit__(self, *exc):
        await self.session.stop()
        await self.task
        await self.gw.stop()

    def live(self, symbol):
        p = self.pub.books.get(symbol)
        return p is not None and not p["stale"]

    def matches_sim(self, symbol):
        p = self.pub.books.get(symbol)
        return p is not None and (p["bid"], p["ask"]) == (self.md.best(symbol, BID), self.md.best(symbol, OFFER))


def test_snapshot_and_reference_data_are_published():
    async def scenario():
        async with Harness() as h:
            await wait_for(lambda: all(h.live(s) for s in SYMBOLS))
            for s in SYMBOLS:
                assert h.matches_sim(s)
                p = h.pub.books[s]
                assert p["bid"] < p["ask"] and p["mid"] == (p["bid"] + p["ask"]) / 2
            assert h.pub.instruments[SYMBOLS[0]]["tick_size"] == 0.25
            assert h.pub.instruments[SYMBOLS[1]]["high_limit"] == 100.0

    run(scenario())


def test_book_tracks_the_exchange_through_many_updates():
    async def scenario():
        async with Harness() as h:
            await wait_for(lambda: all(h.live(s) for s in SYMBOLS))
            for _ in range(200):
                h.md.step()
                await asyncio.sleep(0)
            await wait_for(lambda: all(h.matches_sim(s) for s in SYMBOLS))

    run(scenario())


def test_trades_are_reported():
    async def scenario():
        async with Harness() as h:
            await wait_for(lambda: h.live(SYMBOLS[0]))
            price = h.md.trade(SYMBOLS[0], BUY, 1)
            h.md.flush()
            await wait_for(lambda: (h.pub.books[SYMBOLS[0]]["last_trade"] or {}).get("price") == price)
            assert h.pub.books[SYMBOLS[0]]["last_trade"]["aggressor"] == "BUY"

    run(scenario())


def test_symbol_filter_limits_the_subscription():
    async def scenario():
        async with Harness(symbols=[SYMBOLS[1]]) as h:
            await wait_for(lambda: h.live(SYMBOLS[1]))
            await asyncio.sleep(0.2)
            assert set(h.pub.books) == {SYMBOLS[1]}
            assert set(h.md.subscriptions[SENDER]) == {SYMBOLS[1]}

    run(scenario())


def test_disconnect_marks_books_stale_and_reconnect_refreshes_them():
    async def scenario():
        async with Harness() as h:
            await wait_for(lambda: all(h.live(s) for s in SYMBOLS))
            h.gw.drop(SENDER)
            await wait_for(lambda: all(h.pub.books[s]["stale"] for s in SYMBOLS))
            h.md.move_fair(SYMBOLS[0], 3)  # the market moves while we are away
            await wait_for(lambda: all(h.live(s) for s in SYMBOLS), timeout=8)
            assert all(h.matches_sim(s) for s in SYMBOLS)

    run(scenario())
