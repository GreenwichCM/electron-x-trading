import asyncio
import json

from fakeredis import FakeAsyncRedis
from trading_core import BLOCKS_KEY, Limits, TradingRules, decode_blocks
from trading_core.rules import Trader
from trading_riskmonitor import SNAPSHOT_KEY, RiskEngine, follow_streams, publish

X, Y = "BFUT500-ERCOT-HB_NORTH-261231-15", "BFUT100-PJM-WESTERN_HUB-261231-15"


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def order(cl, trader):
    return {"cl_ord_id": cl, "trader": trader}


def fill(exec_id, cl, side, qty, price, symbol=X):
    return {"exec_id": exec_id, "cl_ord_id": cl, "symbol": symbol, "side": side, "qty": qty, "price": price}


def book(bid, ask, symbol=X):
    return {"symbol": symbol, "bid": bid, "ask": ask, "stale": False, "last_trade": None}


def test_pnl_per_trader_and_for_the_desk():
    e = RiskEngine(TradingRules.everyone(["alice", "dave"], max_loss=1000, max_position=10), clock=Clock())
    e.on_order(order("A1", "alice"))
    e.on_order(order("D1", "dave"))
    e.on_fill(fill("E1", "A1", "BUY", 2, 40.0))
    e.on_fill(fill("E2", "D1", "SELL", 1, 41.0))
    e.on_book(book(42.0, 43.0))  # mark 42.5
    snap = e.snapshot()
    alice, dave = snap["traders"]["alice"], snap["traders"]["dave"]
    assert alice["positions"][X]["qty"] == 2 and alice["unrealized"] == 5.0  # 2 x (42.5 - 40)
    assert dave["unrealized"] == -1.5  # short 1 from 41, now 42.5
    assert snap["desk"]["total"] == 3.5 and snap["desk"]["positions"] == {X: 1}
    assert alice["positions"][X]["mark_source"] == "mid" and alice["limits"]["position"]["pct"] == 20.0


def test_fills_wait_for_their_order_and_duplicates_count_once():
    e = RiskEngine(clock=Clock())
    e.on_fill(fill("E1", "A1", "BUY", 1, 40.0))  # the fill arrived before orderentry's update
    e.on_fill(fill("E1", "A1", "BUY", 1, 40.0))  # and again, resent
    assert e.snapshot()["traders"] == {} and e.snapshot()["unattributed_fills"] == 1
    e.on_order(order("A1", "alice"))
    assert e.snapshot()["traders"]["alice"]["positions"][X]["qty"] == 1


def test_loss_limit_breach():
    e = RiskEngine(TradingRules.everyone(["alice"], max_loss=10), clock=Clock())
    e.on_order(order("A1", "alice"))
    e.on_fill(fill("E1", "A1", "BUY", 2, 50.0))
    e.on_book(book(44.0, 45.0))  # mark 44.5: loss 11
    snap = e.snapshot()
    assert snap["traders"]["alice"]["limits"]["loss"]["pct"] == 110.0
    assert "reached the daily limit" in e.breaches(snap)["alice"]


def test_limits_come_from_the_rules_per_trader_and_contract():
    e = RiskEngine(clock=Clock())  # no rules loaded yet: no limits, so no usage and no breach
    e.on_order(order("A1", "alice"))
    e.on_fill(fill("E1", "A1", "BUY", 4, 50.0))
    e.on_fill(fill("E2", "A1", "BUY", 3, 50.0, symbol=Y))
    e.on_book(book(40.0, 41.0))
    snap = e.snapshot()
    assert snap["traders"]["alice"]["limits"] == {"loss": None, "position": None} and e.breaches(snap) == {}

    rules = TradingRules.everyone(["alice"], max_position=50, max_loss=100)
    rules.limits[("alice", "PJM", "*")] = Limits(max_position=5)  # tighter in PJM
    e.set_rules(rules)
    limits = e.snapshot()["traders"]["alice"]["limits"]
    assert limits["position"] == {"used": 3, "limit": 5, "pct": 60.0, "symbol": Y}  # 3/5 beats 4/50
    assert limits["loss"]["limit"] == 100


def test_mark_age_is_reported():
    clock = Clock()
    e = RiskEngine(clock=clock)
    e.on_order(order("A1", "alice"))
    e.on_fill(fill("E1", "A1", "BUY", 1, 40.0, symbol=Y))
    e.on_book(book(40.0, 41.0, symbol=Y))
    clock.now += 12
    assert e.snapshot()["traders"]["alice"]["positions"][Y]["mark_age_s"] == 12.0


def test_streams_snapshot_and_automatic_block_through_redis():
    async def scenario():
        r = FakeAsyncRedis()
        await r.xadd("orders", {"data": json.dumps(order("A1", "alice"))})
        await r.xadd("fills", {"data": json.dumps(fill("E1", "A1", "BUY", 2, 50.0))})
        e = RiskEngine(TradingRules.everyone(["alice"], max_loss=10))
        e.on_book(book(44.0, 45.0))
        tasks = [asyncio.create_task(follow_streams(r, e)), asyncio.create_task(publish(r, e, every_s=0.05))]
        for _ in range(100):
            if await r.hget(BLOCKS_KEY, "alice"):
                break
            await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        snap = json.loads(await r.get(SNAPSHOT_KEY))
        assert snap["traders"]["alice"]["total"] == -11.0
        blocks = decode_blocks(await r.hgetall(BLOCKS_KEY))
        assert blocks["alice"].by == "riskmonitor" and "loss" in blocks["alice"].reason
        assert len(await r.xrange("alerts")) == 1  # blocked once, not once per second

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_fills_from_the_electronx_screen_go_to_the_trader_with_that_electronx_id():
    clock = Clock()
    e = RiskEngine(clock=clock)
    traders = {"alice": Trader("alice", "firms/h/users/alice"), "dave": Trader("dave")}
    e.set_rules(TradingRules(traders=traders, entitlements=frozenset(), limits={}))
    e.on_order(order("A1", "alice"))
    e.on_fill(fill("E1", "A1", "BUY", 2, 40.0))  # sent from our app
    e.on_fill({**fill("E2", "UI-1", "BUY", 3, 41.0), "user": "firms/h/users/alice"})  # from the screen
    e.on_fill({**fill("E3", "UI-2", "SELL", 1, 41.0), "user": "firms/h/users/nobody"})
    e.on_book(book(41.0, 41.5))
    snap = e.snapshot()
    assert snap["traders"]["alice"]["positions"][X]["qty"] == 2 and snap["unattributed_fills"] == 2

    clock.now += 5  # orderentry never published those orders: they came from the screen
    snap = e.snapshot()
    p = snap["traders"]["alice"]["positions"][X]
    assert (p["qty"], p["screen_qty"]) == (5, 3)
    assert snap["unattributed_fills"] == 1  # nobody has that ElectronX id


def test_only_the_contracts_held_are_read_again():
    async def scenario():
        from trading_riskmonitor.redis_io import refresh_instruments

        redis = FakeAsyncRedis()
        e = RiskEngine(clock=Clock())
        e.on_order(order("A1", "alice"))
        e.on_fill(fill("E1", "A1", "BUY", 1, 40.0))
        await redis.set(f"instrument:{X}", json.dumps({"symbol": X, "high_limit": 60.0}))
        await redis.set(f"instrument:{Y}", json.dumps({"symbol": Y, "high_limit": 70.0}))  # not held
        task = asyncio.create_task(refresh_instruments(redis, e, every_s=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        assert e.instruments[X]["high_limit"] == 60.0 and Y not in e.instruments

    asyncio.run(scenario())
