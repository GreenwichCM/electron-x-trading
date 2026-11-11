"""Orderentry restarts: the state file, the stream tail, and catching up with the exchange."""

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime, timedelta

from fakeredis import FakeAsyncRedis
from trading_core import KillSwitchState, TradingRules
from trading_fix import FixMessage, FixSession, SeqStore, SessionConfig, fix_week_start
from trading_orderentry import (
    Instrument,
    MemorySink,
    Order,
    OrderEntryApp,
    Recovered,
    RedisOrderSink,
    Status,
    load_state,
    recover,
    save_state,
)
from trading_orderentry.recovery import apply
from trading_sim import BID, SELL, SimGateway, SimInstrument, SimMarketData, SimOrderEntry

SENDER = "TEST-OE01"
SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


def update(cl, version, cum_qty=0, status="new", side="BUY", updated=None):
    order = Order(cl, "alice", SYMBOL, side, 5, 40.0, "GTC", status=Status(status), cum_qty=cum_qty, version=version)
    if updated:
        order.updated = updated
    return order.to_dict()


# ---- replay rules ------------------------------------------------------------------------------


def test_an_update_seen_twice_or_out_of_order_changes_nothing():
    rec = Recovered()
    assert apply(rec, update("A", 1))
    assert apply(rec, update("A", 2, cum_qty=2, status="partially_filled"))
    apply(rec, update("A", 2, cum_qty=2, status="partially_filled"))  # the same update again
    assert not apply(rec, update("A", 1))  # an older one, read late
    assert rec.orders["A"].status == Status.PARTIALLY_FILLED
    assert rec.positions == {("alice", SYMBOL): 2}
    apply(rec, update("B", 1, cum_qty=3, status="filled", side="SELL"))
    assert rec.positions == {("alice", SYMBOL): -1}


def test_state_file_keeps_live_and_recent_orders(tmp_path):
    now = time.time()
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    orders = [
        Order.from_dict(update("LIVE", 3, updated=old)),
        Order.from_dict(update("OLD", 4, status="canceled", updated=old)),
        Order.from_dict(update("RECENT", 2, cum_qty=5, status="filled")),
    ]
    save_state(tmp_path / "orders.json", orders, {("alice", SYMBOL): 5, ("bob", SYMBOL): 0}, "1-0", now=now)
    rec = load_state(tmp_path / "orders.json")
    assert set(rec.orders) == {"LIVE", "RECENT"}
    assert rec.orders["LIVE"].version == 3 and rec.positions == {("alice", SYMBOL): 5}
    assert rec.stream_id == "1-0" and rec.last_seen == now


def test_recover_replays_the_stream_after_the_file(tmp_path):
    async def scenario():
        r = FakeAsyncRedis()
        first = await r.xadd("orders", {"data": json.dumps(update("A", 1))})
        await r.xadd("orders", {"data": json.dumps(update("A", 2, cum_qty=1, status="partially_filled"))})
        await r.xadd("orders", {"data": json.dumps(update("B", 1))})

        everything = await recover(r, tmp_path / "missing.json", page=1)  # no file: the whole stream
        assert everything.replayed == 3 and everything.positions == {("alice", SYMBOL): 1}

        path = tmp_path / "orders.json"
        save_state(path, [Order.from_dict(update("A", 1))], {}, first.decode())
        rec = await recover(r, path)
        assert rec.replayed == 2  # only the entries after the one the file includes
        assert rec.orders["A"].cum_qty == 1 and set(rec.orders) == {"A", "B"}
        assert rec.positions == {("alice", SYMBOL): 1}

    asyncio.run(scenario())


def test_from_earlier_week():
    reset = fix_week_start().timestamp()
    assert Recovered(last_seen=reset - 60).from_earlier_week()
    assert not Recovered(last_seen=reset + 60).from_earlier_week()
    assert not Recovered().from_earlier_week()  # nothing recorded at all


# ---- execution reports after a restart -----------------------------------------------------------


def execution_report(cl, exec_type, status, cum_qty, orig=None):
    fields = [(35, "8"), (11, cl), (37, "O1"), (17, "E"), (150, exec_type), (39, status), (14, str(cum_qty))]
    return FixMessage(fields + ([(41, orig)] if orig else []))


def restored_app(*orders):
    oe = OrderEntryApp(MemorySink(), account="A", default_trader="t")
    rec = Recovered()
    for o in orders:
        apply(rec, o)
    oe.restore(rec)
    return oe


def test_reports_after_a_restart_are_matched_and_counted_once():
    oe = restored_app(update("A", 3, status="pending_cancel"), update("B", 2, cum_qty=1, status="partially_filled"))
    # the cancel's report: our cancel ClOrdID from before the restart is unknown, OrigClOrdID is not
    oe.on_app_message(None, execution_report("CXL-from-before", "4", "4", 0, orig="A"))
    assert oe.orders["A"].status == Status.CANCELED

    oe.on_app_message(None, execution_report("B", "F", "1", 3))  # 2 more filled
    oe.on_app_message(None, execution_report("B", "F", "1", 3))  # the same report again
    oe.on_app_message(None, execution_report("B", "0", "0", 0))  # an older one
    assert oe.orders["B"].cum_qty == 3 and oe.positions[("alice", SYMBOL)] == 3
    assert oe.orders["B"].status == Status.PARTIALLY_FILLED


# ---- the whole thing, against the simulator ------------------------------------------------------


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


class Exchange:
    def __init__(self):
        self.md = SimMarketData(SimGateway({}), [SimInstrument(SYMBOL, fair=45.0)], seed=3)
        self.gw = SimGateway({SENDER: "sim"})
        self.oe = SimOrderEntry(self.gw, self.md)

    def bid(self):
        return self.md.best(SYMBOL, BID)


class OrderEntry:
    """One run of orderentry: app, Redis sink and FIX session, with its files in `state_dir`."""

    def __init__(self, redis, port, state_dir):
        self.sink = RedisOrderSink(redis)
        self.app = OrderEntryApp(
            self.sink,
            account="TA-TEST",
            default_trader="trader@example.com",
            rules=TradingRules.everyone(
                ["alice"], max_order_qty=10, max_order_notional=5000, max_open_orders=20, max_position=50
            ),
            instruments={SYMBOL: Instrument(SYMBOL, 0.25, low_limit=0.0, high_limit=500.0, state="OPEN")},
        )
        cfg = SessionConfig(
            name="oe", host="127.0.0.1", port=port, sender_comp_id=SENDER, target_comp_id="EXI", password="sim"
        )
        self.session = FixSession(cfg, self.app, SeqStore(state_dir / "seq.json"))
        self.path = state_dir / "orders.json"

    async def start(self, rec=None, from_earlier_week=False):
        if rec is not None:
            self.app.restore(rec, from_earlier_week)
        self.tasks = [asyncio.create_task(self.sink.run()), asyncio.create_task(self.session.run())]
        await wait_for(lambda: self.session.logged_on)

    async def flushed(self):
        await wait_for(lambda: self.sink._queue.empty())
        await asyncio.sleep(0.05)

    def save(self):
        save_state(self.path, self.app.orders.values(), self.app.positions, self.sink.last_id)

    async def crash(self, exchange):
        """Gone without a Logout and without saving the state file."""
        exchange.gw.drop(SENDER)
        for task in self.tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self.tasks)
        await wait_for(lambda: exchange.gw.session(SENDER).conn is None)


def test_crash_and_restart_mid_week(tmp_path):
    async def scenario():
        ex = Exchange()
        port = await ex.gw.start()
        redis = FakeAsyncRedis()

        first = OrderEntry(redis, port, tmp_path)
        await first.start()
        far = first.app.submit("alice", SYMBOL, "BUY", 2, ex.bid() - 1.0)
        near = first.app.submit("alice", SYMBOL, "BUY", 3, ex.bid() + 0.25)  # best bid: next to fill
        await wait_for(lambda: far.status == Status.NEW and near.status == Status.NEW)
        await first.flushed()
        first.save()  # the last save before the crash...
        late = first.app.submit("alice", SYMBOL, "BUY", 1, ex.bid() - 2.0)  # ...so this is only in the stream
        await wait_for(lambda: late.status == Status.NEW)
        await first.flushed()
        await first.crash(ex)

        ex.md.trade(SYMBOL, SELL, 1)  # while orderentry is down, someone sells into our bid
        ex.md.flush()

        rec = await recover(redis, first.path)
        assert set(rec.orders) == {far.cl_ord_id, near.cl_ord_id, late.cl_ord_id}
        second = OrderEntry(redis, port, tmp_path)
        await second.start(rec)
        n = second.app.orders[near.cl_ord_id]
        await wait_for(lambda: n.cum_qty == 1)  # the exchange resent the fill after logon
        assert n.status == Status.PARTIALLY_FILLED
        assert second.app.positions == {("alice", SYMBOL): 1}

        # the kill switch reaches orders from before the restart
        second.app.set_kill_switch(KillSwitchState(active=True, reason="test", by="bob"))
        orders = [second.app.orders[o.cl_ord_id] for o in (far, near, late)]
        await wait_for(lambda: all(o.status == Status.CANCELED for o in orders))
        assert not ex.md.books[SYMBOL].keys() & {o.order_id for o in orders}
        await second.crash(ex)
        await ex.gw.stop()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_orders_from_an_earlier_week_are_cancelled(tmp_path):
    async def scenario():
        ex = Exchange()
        port = await ex.gw.start()
        redis = FakeAsyncRedis()
        first = OrderEntry(redis, port, tmp_path)
        await first.start()
        resting = first.app.submit("alice", SYMBOL, "BUY", 2, ex.bid() - 1.0)
        await wait_for(lambda: resting.status == Status.NEW)
        await first.flushed()
        await first.crash(ex)

        rec = await recover(redis, first.path)
        apply(rec, update("GONE", 1))  # an order the exchange no longer has
        second = OrderEntry(redis, port, tmp_path)
        await second.start(rec, from_earlier_week=True)
        second.app.cancel_unconfirmed()
        kept, gone = second.app.orders[resting.cl_ord_id], second.app.orders["GONE"]
        await wait_for(lambda: kept.status == Status.CANCELED and gone.status == Status.CANCELED)
        assert "not at the exchange" in gone.text
        await second.crash(ex)
        await ex.gw.stop()

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_a_bust_replayed_from_the_stream_moves_the_position_back():
    rec = Recovered()
    filled = Order("C1", "alice", SYMBOL, "BUY", 2, 45.0, "DAY", status=Status.FILLED, cum_qty=2, version=2)
    apply(rec, filled.to_dict())
    busted = Order(
        "C1", "alice", SYMBOL, "BUY", 2, 45.0, "DAY", status=Status.FILLED, cum_qty=2, version=3, busted_qty=1
    )
    busted.busted = ["E1"]
    apply(rec, busted.to_dict())
    apply(rec, filled.to_dict())  # older: ignored
    assert rec.positions[("alice", SYMBOL)] == 1 and rec.orders["C1"].busted == ["E1"]
