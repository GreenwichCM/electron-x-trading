"""MarketDataApp against the simulated MD gateway, over real TCP on 127.0.0.1."""

import asyncio
import json
import time
from datetime import UTC, datetime

from trading_fix import FixSession, SeqStore, SessionConfig
from trading_marketdata import MarketDataApp, MemoryPublisher
from trading_sim import BID, BUY, OFFER, SELL, SimGateway, SimInstrument, SimMarketData

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
    def __init__(self, symbols=(), instruments=INSTRUMENTS, **app_options):
        self.symbols = symbols
        self.instruments = instruments
        self.app_options = app_options

    async def __aenter__(self):
        self.gw = SimGateway({SENDER: "sim"})
        self.md = SimMarketData(self.gw, self.instruments, seed=7)
        port = await self.gw.start()
        self.pub = MemoryPublisher()
        self.app = MarketDataApp(self.pub, self.symbols, **self.app_options)
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
            # and on the time & sales, once, at the exchange's time
            await wait_for(lambda: len(h.pub.trades) == 1)
            tape = h.pub.trades[0]
            assert (tape["symbol"], tape["price"], tape["qty"], tape["aggressor"]) == (SYMBOLS[0], price, 1.0, "BUY")
            assert tape["trade_id"] == h.pub.books[SYMBOLS[0]]["last_trade"]["trade_id"] != ""
            assert tape["time"].endswith("+00:00")
            h.md.trade(SYMBOLS[0], SELL, 1)
            h.md.flush()
            await wait_for(lambda: len(h.pub.trades) == 2)
            assert h.pub.trades[1]["aggressor"] == "SELL"

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


def test_trading_times_state_and_settlement_are_published():
    async def scenario():
        ends = time.time() + 600
        ending = SimInstrument("BFUT100-ERCOT-HB_HOUSTON-261110-15", fair=42.0, high=100.0, last_trade_at=ends)
        ending.settle_after_s = 0
        async with Harness(instruments=[*INSTRUMENTS, ending]) as h:
            s = ending.symbol
            await wait_for(lambda: h.live(s))
            inst = h.pub.instruments[s]
            when = datetime.fromtimestamp(int(ends), UTC).isoformat()
            assert inst["last_trade_time"] == inst["expiration_time"] == when
            assert h.pub.instruments[SYMBOLS[0]]["last_trade_time"] is None
            assert h.pub.books[s]["state"] == "OPEN" and h.pub.books[s]["settlement"] is None

            ending.last_trade_at = time.time() - 1  # trading ends, and the settlement follows at once
            h.md.step()
            await wait_for(lambda: (h.pub.books[s]["settlement"] or {}).get("price") == 42.0)
            p = h.pub.books[s]
            assert p["state"] == "EXPIRED" and p["settlement"]["final"] and p["bid"] is None and p["ask"] is None

    run(scenario())


def test_new_contracts_are_picked_up_and_old_ones_retired():
    async def scenario():
        gone = time.time() - 1
        settles = SimInstrument("BFUT100-ERCOT-HB_HOUSTON-261110-15", high=100.0, last_trade_at=gone, settle_after_s=0)
        waits = SimInstrument("BFUT100-PJM-WESTERN_HUB-261110-15", high=100.0, last_trade_at=gone, settle_after_s=1e9)
        async with Harness(instruments=[*INSTRUMENTS, settles, waits]) as h:
            h.app.set_interest((), {settles.symbol, waits.symbol})  # trading has ended: followed because held
            await wait_for(lambda: all(h.live(s) for s in [*SYMBOLS, settles.symbol, waits.symbol]))
            h.md.step()  # both stop trading; one settles at once, PJM-style the other does not
            await wait_for(lambda: ((h.pub.books[settles.symbol]["settlement"]) or {}).get("final"))

            new = SimInstrument("BFUT500-ERCOT-HB_NORTH-261231-16", fair=46.0)
            h.md.list_instrument(new)
            h.md.delist(settles.symbol)
            h.md.delist(waits.symbol)
            before = dict(h.app.subscribed)
            h.app.request_list(h.session)
            await wait_for(lambda: h.live(new.symbol))
            assert all(h.app.subscribed[s] == before[s] for s in SYMBOLS)  # no new subscriptions for these
            assert h.pub.retired == [settles.symbol] and settles.symbol not in h.app.books
            assert waits.symbol in h.app.subscribed and waits.symbol in h.app.delisted  # waiting for settlement

            h.gw.drop(SENDER)  # after reconnecting, the exchange rejects the old contract: it is retired
            await wait_for(lambda: waits.symbol in h.pub.retired, timeout=8)
            await wait_for(lambda: all(h.live(s) for s in [*SYMBOLS, new.symbol]), timeout=8)
            assert set(h.app.subscribed) == {*SYMBOLS, new.symbol}

    run(scenario())


def test_a_long_security_list_comes_in_fragments():
    async def scenario():
        many = [SimInstrument(f"BFUT500-ERCOT-HB_T{n}-261231-15") for n in range(450)]  # three fragments of 200
        async with Harness(instruments=many, subscribe_per_s=1000) as h:
            await wait_for(lambda: len(h.app.subscribed) == 450, timeout=10)
            assert len(h.pub.instruments) == 450

    run(scenario())


def test_subscriptions_go_out_a_batch_a_second():
    async def scenario():
        many = [SimInstrument(f"BFUT500-ERCOT-HB_T{n}-261231-15") for n in range(25)]
        async with Harness(instruments=many, subscribe_per_s=10) as h:
            requests = lambda: sum(1 for m in h.gw.session(SENDER).received if m.msg_type == "V")  # noqa: E731
            await wait_for(lambda: requests() == 10)
            await asyncio.sleep(0.5)
            assert requests() == 10  # the next ten wait a second
            await wait_for(lambda: requests() == 25, timeout=4)
            await wait_for(lambda: all(h.live(i.symbol) for i in many))

    run(scenario())


def test_contracts_on_a_screen_and_held_are_subscribed_first():
    async def scenario():
        many = [SimInstrument(f"BFUT500-ERCOT-HB_T{n:02d}-261231-15") for n in range(40)]
        screen, held = {many[30].symbol, many[31].symbol}, {many[39].symbol}
        async with Harness(instruments=many, subscribe_per_s=5) as h:
            h.app.set_interest(screen, held)  # before the first batch: they jump the queue
            await wait_for(lambda: all(h.live(s) for s in screen | held))
            assert len(h.app.subscribed) <= 10  # the rest still follow, five a second

        async with Harness(instruments=many, subscribe_per_s=5, background=False) as h:
            await asyncio.sleep(0.6)
            assert h.app.subscribed == {}  # nothing on a screen or held yet: nothing at all
            h.app.set_interest(screen, held)
            await wait_for(lambda: all(h.live(s) for s in screen | held))
            await asyncio.sleep(1.2)
            assert set(h.app.subscribed) == screen | held
            h.app.set_interest(screen | {many[0].symbol}, held)  # a browser shows one more
            await wait_for(lambda: h.live(many[0].symbol))

    run(scenario())


def test_contracts_whose_trading_ended_are_not_subscribed_in_the_background():
    async def scenario():
        ended = SimInstrument("BFUT500-ERCOT-HB_NORTH-261110-03", last_trade_at=time.time() - 60)
        soon = SimInstrument("BFUT500-ERCOT-HB_NORTH-261231-14", last_trade_at=time.time() + 600)
        async with Harness(instruments=[*INSTRUMENTS, ended, soon], subscribe_per_s=2) as h:
            await wait_for(lambda: len(h.app.subscribed) == 3, timeout=8)  # two that never end, and soon
            assert ended.symbol not in h.app.subscribed and ended.symbol in h.pub.instruments  # listed, not followed
            assert next(iter(h.app.subscribed)) == soon.symbol  # the soonest to stop trading comes first
            h.app.set_interest({ended.symbol}, ())  # someone looks at it anyway
            await wait_for(lambda: h.live(ended.symbol))

    run(scenario())


def test_interest_is_read_in_one_call_and_entries_of_stopped_apis_are_ignored():
    async def scenario():
        from fakeredis import FakeAsyncRedis
        from trading_marketdata.__main__ import follow_interest

        r = FakeAsyncRedis()
        now = time.time()
        await r.hset(
            "md:wanted",
            mapping={
                "api-1": json.dumps({"symbols": ["A", "B"], "time": now}),
                "stopped": json.dumps({"symbols": ["Z"], "time": now - 300}),  # did not clean up after itself
            },
        )
        await r.set("dc:positions", json.dumps({"positions": {"C": 2, "D": 0}}))
        seen = []

        class App:
            def set_interest(self, on_screen, held):
                seen.append((set(on_screen), set(held)))

        task = asyncio.create_task(follow_interest(r, App(), every_s=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        assert seen[0] == ({"A", "B"}, {"C"})

    run(scenario())


def test_a_trade_sent_twice_is_on_the_time_and_sales_once():
    from trading_fix import FixMessage
    from trading_marketdata import OrderBook

    pub = MemoryPublisher()
    app = MarketDataApp(pub, [SYMBOLS[0]])
    app.books[SYMBOLS[0]] = OrderBook(SYMBOLS[0])
    app.books[SYMBOLS[0]].stale = False
    trade = [(279, "0"), (269, "2"), (55, SYMBOLS[0]), (270, "41.25"), (271, "3"), (1003, "T1"), (2446, "2")]
    trade += [(272, "20261112"), (273, "15:04:05.123456789")]
    for _ in range(2):
        app._on_incremental(FixMessage([(35, "X"), (268, "1"), *trade]))
    assert pub.trades == [
        {
            "symbol": SYMBOLS[0],
            "price": 41.25,
            "qty": 3.0,
            "aggressor": "SELL",
            "trade_id": "T1",
            "time": "2026-11-12T15:04:05.123+00:00",
        }
    ]
