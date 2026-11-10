"""DropCopyApp against the simulated order entry gateway, which copies every ExecutionReport to the
drop copy session. Orders are sent straight over a FIX order entry session."""

import asyncio
import json
import time

from fakeredis import FakeAsyncRedis
from trading_core import KILLSWITCH_KEY, KillSwitchState
from trading_dropcopy import (
    DropCopyApp,
    ExchangeOrder,
    FillJournal,
    MemoryFillSink,
    Reconciler,
    RedisFillSink,
    trip_kill_switch,
)
from trading_fix import FixSession, SeqStore, SessionConfig, utc_timestamp
from trading_sim import OFFER, SimGateway, SimInstrument, SimMarketData, SimOrderEntry

OE, DC = "TEST-OE01", "TEST-DC01"
SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 20))


class Quiet:
    def on_logon(self, session):
        pass

    def on_disconnect(self, session):
        pass

    def on_app_message(self, session, msg):
        pass


def cfg(port, sender):
    return SessionConfig(
        name=sender, host="127.0.0.1", port=port, sender_comp_id=sender, target_comp_id="EXI", password="sim"
    )


class Harness:
    def __init__(self, journal=None):
        self.journal = journal

    async def __aenter__(self):
        self.md = SimMarketData(SimGateway({}), [SimInstrument(SYMBOL, fair=45.0)], seed=3)
        self.gw = SimGateway({OE: "sim", DC: "sim"})
        self.exchange = SimOrderEntry(self.gw, self.md, drop_copy_senders={DC})
        port = await self.gw.start()
        self.port = port
        self.oe = FixSession(cfg(port, OE), Quiet(), SeqStore())
        self.sink = MemoryFillSink()
        self.app = DropCopyApp(self.sink, self.journal)
        self.dc = FixSession(cfg(port, DC), self.app, SeqStore())
        self.tasks = [asyncio.create_task(self.oe.run()), asyncio.create_task(self.dc.run())]
        await wait_for(lambda: self.oe.logged_on and self.dc.logged_on)
        return self

    async def __aexit__(self, *exc):
        await self.oe.stop()
        await self.dc.stop()
        await asyncio.gather(*self.tasks)
        await self.gw.stop()

    def buy_at_ask(self, cl, qty=1):
        price = self.md.best(SYMBOL, OFFER)
        body = [
            (11, cl),
            (1, "TA"),
            (55, SYMBOL),
            (54, "1"),
            (38, str(qty)),
            (40, "2"),
            (44, f"{price:.2f}"),
            (59, "0"),
            (60, utc_timestamp()),
        ]
        self.oe.send("D", body, header=[(50, "trader@example.com")])


def test_fills_build_exchange_positions():
    async def scenario():
        async with Harness() as h:
            h.buy_at_ask("C1", 2)
            await wait_for(lambda: h.app.positions.get(SYMBOL) == 2)
            assert h.sink.latest_positions == {SYMBOL: 2} and h.sink.connected
            assert h.sink.fills[0]["cl_ord_id"] == "C1" and h.sink.fills[0]["side"] == "BUY"

    run(scenario())


def test_fills_during_a_disconnect_arrive_once_after_reconnect():
    async def scenario():
        async with Harness() as h:
            h.buy_at_ask("C1")
            await wait_for(lambda: h.app.positions.get(SYMBOL) == 1)
            h.gw.drop(DC)
            await wait_for(lambda: not h.app.connected)
            h.buy_at_ask("C2")  # filled while the drop copy is away
            await wait_for(lambda: h.app.positions.get(SYMBOL) == 2, timeout=8)
            await asyncio.sleep(0.3)
            assert h.app.positions[SYMBOL] == 2 and len(h.sink.fills) == 2  # nothing counted twice

    run(scenario())


def test_positions_survive_a_restart(tmp_path):
    async def scenario():
        journal = FillJournal(tmp_path / "fills.jsonl")
        async with Harness(journal) as h:
            h.buy_at_ask("C1", 3)
            await wait_for(lambda: h.app.positions.get(SYMBOL) == 3)
        again = DropCopyApp(MemoryFillSink(), FillJournal(tmp_path / "fills.jsonl"))
        assert again.positions == {SYMBOL: 3} and len(again.seen) >= 1

    run(scenario())


def test_trip_kill_switch_keeps_the_first_reason():
    async def scenario():
        r = FakeAsyncRedis()
        await trip_kill_switch(r, "first break")
        await trip_kill_switch(r, "second break")
        state = KillSwitchState.from_json(await r.get(KILLSWITCH_KEY))
        assert state.active and state.reason == "first break" and state.by == "dropcopy"
        alerts = await r.xrange("alerts")
        assert len(alerts) == 1 and json.loads(alerts[0][1][b"data"])["reason"] == "first break"

    run(scenario())


def test_orders_from_the_electronx_screen_are_followed_with_their_trader():
    async def scenario():
        async with Harness() as h:
            price = h.md.best(SYMBOL, OFFER) + 1.0  # through a few price levels: fills in full
            h.exchange.screen_order("firms/h/users/dave", SYMBOL, "1", 2, price, tif="3")
            await wait_for(lambda: h.app.positions.get(SYMBOL) == 2)
            assert {f["user"] for f in h.sink.fills} == {"firms/h/users/dave"} and h.sink.fills[0]["manual"]
            await wait_for(lambda: next(iter(h.app.orders.values())).status == "filled")
            (screen,) = h.app.orders.values()
            assert (screen.user, screen.side, screen.qty, screen.filled) == ("firms/h/users/dave", "BUY", 2, 2)

            h.buy_at_ask("C1")  # ours: sent over FIX, with SenderSubID trader@example.com
            await wait_for(lambda: h.app.positions.get(SYMBOL) == 3)
            assert h.sink.fills[-1]["user"] == "trader@example.com"

            reconciler = Reconciler(screen_after_s=0)
            reconciler.on_order({"cl_ord_id": "C1", "symbol": SYMBOL, "side": "BUY", "cum_qty": 1})
            split = reconciler.split(h.app.orders.values())
            assert split.ours == {SYMBOL: 1} and split.screen == {SYMBOL: 2} and split.screen_orders == [screen]
            assert screen.to_dict()["source"] == "electronx" and screen.to_dict()["exchange_user"] == screen.user

    run(scenario())


def test_published_positions_are_split_as_of_the_moment_they_are_published():
    async def scenario():
        r = FakeAsyncRedis()
        clock = [10.0]
        reconciler = Reconciler(screen_after_s=2, clock=lambda: clock[0])
        orders = [ExchangeOrder("O1", "UI-1", "firms/h/users/dave", SYMBOL, filled=2, first_seen=10.0)]
        sink = RedisFillSink(r, classify=lambda: reconciler.split(orders))
        task = asyncio.create_task(sink.run())
        sink.positions({SYMBOL: 2}, True)  # the fill has just arrived: whose it is is not known yet
        clock[0] = 13.0
        assert sink.changed(reconciler.split(orders))  # now it is the screen's: publish again
        sink.positions({SYMBOL: 2}, True)
        await sink.close()
        await task
        latest = json.loads(await r.get("dc:positions"))
        assert latest["screen"] == {SYMBOL: 2} and latest["pending"] == {}
        assert latest["screen_by_user"] == {"firms/h/users/dave": {SYMBOL: 2}}
        assert not sink.changed(reconciler.split(orders))

    run(scenario())
