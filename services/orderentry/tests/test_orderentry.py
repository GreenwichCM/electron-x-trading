"""OrderEntryApp against the simulated order entry gateway, over real TCP on 127.0.0.1."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from trading_fix import FixSession, SeqStore, SessionConfig
from trading_orderentry import (
    Instrument,
    MemorySink,
    OrderEntryApp,
    OrderError,
    RiskLimits,
    Status,
    create_control_app,
)
from trading_sim import BID, OFFER, SELL, SimGateway, SimInstrument, SimMarketData, SimOrderEntry

SENDER = "TEST-OE01"
SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"
TOKEN = "t" * 32


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 20))


class Harness:
    async def __aenter__(self):
        self.md = SimMarketData(SimGateway({}), [SimInstrument(SYMBOL, fair=45.0)], seed=3)
        self.gw = SimGateway({SENDER: "sim"})
        self.exchange = SimOrderEntry(self.gw, self.md)
        port = await self.gw.start()
        self.sink = MemorySink()
        self.oe = OrderEntryApp(
            self.sink,
            account="TA-TEST",
            default_trader="trader@example.com",
            limits=RiskLimits(max_order_qty=10, max_order_notional=5000, max_open_orders=20, max_position=50),
            instruments={SYMBOL: Instrument(SYMBOL, 0.25, low_limit=0.0, high_limit=500.0, state="OPEN")},
        )
        cfg = SessionConfig(
            name="oe", host="127.0.0.1", port=port, sender_comp_id=SENDER, target_comp_id="EXI", password="sim"
        )
        self.session = FixSession(cfg, self.oe, SeqStore())
        self.task = asyncio.create_task(self.session.run())
        await wait_for(lambda: self.session.logged_on)
        return self

    async def __aexit__(self, *exc):
        await self.session.stop()
        await self.task
        await self.gw.stop()

    def best(self, side):
        return self.md.best(SYMBOL, side)

    def sent(self, msg_type):
        return sum(1 for m in self.gw.session(SENDER).received if m.msg_type == msg_type)


def test_passive_order_rests_on_the_book():
    async def scenario():
        async with Harness() as h:
            price = h.best(BID) - 0.5
            order = h.oe.submit("alice", SYMBOL, "BUY", 2, price)
            await wait_for(lambda: order.status == Status.NEW)
            assert order.order_id in h.md.books[SYMBOL]
            msg = h.gw.session(SENDER).received[-1]
            assert msg.get(50) == "trader@example.com" and msg.get(1) == "TA-TEST" and msg.get(44) == f"{price:.2f}"

    run(scenario())


def test_aggressive_order_fills_at_the_best_offer():
    async def scenario():
        async with Harness() as h:
            ask = h.best(OFFER)
            order = h.oe.submit("alice", SYMBOL, "BUY", 1, ask)
            await wait_for(lambda: order.status == Status.FILLED)
            assert order.cum_qty == 1 and order.avg_px == ask
            assert h.oe.positions[("alice", SYMBOL)] == 1

    run(scenario())


def test_resting_order_is_filled_by_another_trader():
    async def scenario():
        async with Harness() as h:
            price = h.best(BID) + 0.25  # improve the bid, so we are first in line
            assert price < h.best(OFFER)
            order = h.oe.submit("alice", SYMBOL, "BUY", 3, price)
            await wait_for(lambda: order.status == Status.NEW)
            h.md.trade(SYMBOL, SELL, 1)  # someone sells into our bid
            h.md.flush()
            await wait_for(lambda: order.status == Status.PARTIALLY_FILLED)
            assert order.cum_qty == 1 and order.leaves == 2

    run(scenario())


def test_cancel():
    async def scenario():
        async with Harness() as h:
            order = h.oe.submit("alice", SYMBOL, "SELL", 2, h.best(OFFER) + 1.0)
            await wait_for(lambda: order.status == Status.NEW)
            h.oe.cancel("alice", order.cl_ord_id)
            assert order.status == Status.PENDING_CANCEL
            await wait_for(lambda: order.status == Status.CANCELED)
            assert order.order_id not in h.md.books[SYMBOL]
            with pytest.raises(OrderError) as exc:
                h.oe.cancel("alice", order.cl_ord_id)
            assert exc.value.status_code == 409

    run(scenario())


def test_only_the_owner_can_cancel():
    async def scenario():
        async with Harness() as h:
            order = h.oe.submit("alice", SYMBOL, "SELL", 2, h.best(OFFER) + 1.0)
            await wait_for(lambda: order.status == Status.NEW)
            with pytest.raises(OrderError) as exc:
                h.oe.cancel("bob", order.cl_ord_id)
            assert exc.value.status_code == 403

    run(scenario())


def test_risk_rejects_never_reach_the_exchange():
    async def scenario():
        async with Harness() as h:
            too_big = h.oe.submit("alice", SYMBOL, "BUY", 11, 40.0)
            off_tick = h.oe.submit("alice", SYMBOL, "BUY", 1, 40.1)
            unknown = h.oe.submit("alice", "NOPE", "BUY", 1, 40.0)
            await asyncio.sleep(0.2)
            assert [o.status for o in (too_big, off_tick, unknown)] == [Status.REJECTED] * 3
            assert "limit" in too_big.text and "tick" in off_tick.text and "unknown" in unknown.text
            assert h.sent("D") == 0
            assert h.sink.latest(too_big.cl_ord_id)["status"] == "rejected"

    run(scenario())


def test_ioc_remainder_is_cancelled():
    async def scenario():
        async with Harness() as h:
            ticks = h.md.best_ticks(SYMBOL, OFFER) - 1  # a new best offer of 1 lot, still above the bid
            assert ticks > h.md.best_ticks(SYMBOL, BID)
            h.md.add_order(SYMBOL, OFFER, ticks, 1)
            h.md.flush()
            order = h.oe.submit("alice", SYMBOL, "BUY", 3, ticks * 0.25, "IOC")
            await wait_for(lambda: order.status == Status.CANCELED)
            assert order.cum_qty == 1 and order.leaves == 0

    run(scenario())


def test_orders_are_rejected_while_disconnected():
    async def scenario():
        async with Harness() as h:
            h.gw.drop(SENDER)
            await wait_for(lambda: not h.session.logged_on)
            order = h.oe.submit("alice", SYMBOL, "BUY", 1, 40.0)
            assert order.status == Status.REJECTED and "not connected" in order.text

    run(scenario())


def test_control_endpoint_needs_the_internal_token():
    oe = OrderEntryApp(MemorySink(), account="A", default_trader="t")
    client = TestClient(create_control_app(oe, TOKEN))
    assert client.get("/orders").status_code == 401
    assert client.get("/orders", headers={"X-Internal-Token": "wrong"}).status_code == 401
    ok = client.post(
        "/orders",
        headers={"X-Internal-Token": TOKEN},
        json={"trader": "alice", "symbol": "X", "side": "BUY", "qty": 1, "price": 1.0},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "rejected"  # no instrument X, no session
    assert len(client.get("/orders", headers={"X-Internal-Token": TOKEN}).json()) == 1
