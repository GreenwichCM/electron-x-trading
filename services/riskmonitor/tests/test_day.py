"""Daily P&L: the trading day, start-of-day marks, the daily loss limit and expired contracts."""

import asyncio
import json
from datetime import UTC, date, datetime

import pytest
from fakeredis import FakeAsyncRedis
from trading_core import TradingRules
from trading_db import ConfigError
from trading_riskmonitor import SOD_KEY, RiskEngine, TradingDay, load_sod, publish
from trading_riskmonitor.settings import Settings

X = "BFUT500-ERCOT-HB_NORTH-261231-15"
EXPIRING = "BFUT500-ERCOT-HB_NORTH-261110-15"  # trading ends 2026-11-10 15:00 New York (20:00 UTC)
DB_ENV = {"DB_HOST": "127.0.0.1", "DB_NAME": "x", "DB_USER": "u", "DB_PASSWORD": "p"}


def utc(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def fix(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def fill(exec_id, side, qty, price, symbol=X, time=""):
    return {
        "exec_id": exec_id,
        "cl_ord_id": "A1",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "time": time,
    }


def book(bid, ask, symbol=X):
    return {"symbol": symbol, "bid": bid, "ask": ask, "stale": False, "last_trade": None}


def engine(clock, max_loss=None):
    rules = TradingRules.everyone(["alice"], max_loss=max_loss) if max_loss else None
    e = RiskEngine(rules, clock=clock)
    e.on_order({"cl_ord_id": "A1", "trader": "alice"})
    return e


def test_trading_day_boundaries():
    ny = TradingDay()
    assert ny.day_of(utc(2026, 11, 11, 4, 59)) == date(2026, 11, 10)  # 23:59 New York
    assert ny.day_of(utc(2026, 11, 11, 5, 0)) == date(2026, 11, 11)
    evening = TradingDay.parse("America/New_York", "17:00")
    assert evening.day_of(utc(2026, 11, 10, 21, 59)) == date(2026, 11, 9)  # 16:59
    assert evening.day_of(utc(2026, 11, 10, 22, 0)) == date(2026, 11, 10)  # 17:00
    assert evening.describe(date(2026, 11, 10))["starts_at"] == "2026-11-10T17:00:00-05:00"
    with pytest.raises(ValueError):
        TradingDay.parse("Mars/Olympus", "00:00")
    with pytest.raises(ValueError):
        TradingDay.parse("UTC", "5pm")


def test_settings_reject_a_bad_day_setting():
    assert Settings.from_env({**DB_ENV, "RISK_DAY_START": "17:00"}).trading_day.start.hour == 17
    with pytest.raises(ConfigError):
        Settings.from_env({**DB_ENV, "RISK_DAY_TZ": "nowhere"})


def test_the_day_rolls_at_midnight_new_york():
    clock = Clock(utc(2026, 11, 10, 15))  # 10:00 New York
    e = engine(clock)
    e.on_fill(fill("E1", "BUY", 2, 40.0))
    e.on_book(book(42.0, 43.0))  # 42.5
    alice = e.snapshot()["traders"]["alice"]
    assert alice["today"]["total"] == 5.0 and alice["total"] == 5.0

    clock.now = utc(2026, 11, 11, 5, 0, 1)  # 00:00:01 New York, the next day
    snap = e.snapshot()
    alice, p = snap["traders"]["alice"], snap["traders"]["alice"]["positions"][X]
    assert snap["trading_day"]["day"] == "2026-11-11"
    assert alice["today"] == {"realized": 0.0, "unrealized": 0.0, "total": 0.0} and alice["total"] == 5.0
    assert (p["sod_qty"], p["sod_mark"], p["sod_mark_late"]) == (2, 42.5, False)

    e.on_book(book(44.0, 45.0))  # 44.5: up 2 a lot today
    assert e.snapshot()["traders"]["alice"]["today"]["total"] == 4.0
    e.on_fill(fill("E2", "SELL", 2, 45.0))
    alice = e.snapshot()["traders"]["alice"]
    assert alice["today"] == {"realized": 5.0, "unrealized": 0.0, "total": 5.0}  # 2 x (45 - 42.5)
    assert alice["realized"] == 10.0 and alice["total"] == 10.0  # 2 x (45 - 40)
    assert e.snapshot()["desk"]["today"]["total"] == 5.0


def test_the_loss_limit_is_for_the_day():
    clock = Clock(utc(2026, 11, 10, 15))
    e = engine(clock, max_loss=10)
    e.on_fill(fill("E1", "BUY", 2, 50.0))
    e.on_book(book(45.5, 46.5))  # 46: down 8
    assert e.breaches(e.snapshot()) == {}
    clock.now = utc(2026, 11, 11, 15)
    e.on_book(book(44.0, 45.0))  # 44.5: down 3 more today, 11 since the start
    snap = e.snapshot()
    assert snap["traders"]["alice"]["limits"]["loss"] == {"used": 3.0, "limit": 10, "pct": 30.0}
    assert snap["traders"]["alice"]["total"] == -11.0 and e.breaches(snap) == {}
    e.on_book(book(40.0, 41.0))  # 40.5: down 11 today
    assert "today's loss 11.00 reached the daily limit" in e.breaches(e.snapshot())["alice"]


def test_after_a_restart_fills_are_sorted_by_their_own_time_and_saved_marks_reused():
    yesterday, now = utc(2026, 11, 10, 15), utc(2026, 11, 11, 15)
    fills = [fill("E1", "BUY", 3, 40.0, time=fix(yesterday)), fill("E2", "SELL", 1, 47.0, time=fix(now - 60))]

    e = engine(Clock(now))  # restarted today, with the start-of-day mark saved before the restart
    assert e.restore_sod({"day": "2026-11-11", "marks": {X: {"price": 45.0, "late": False}}})
    for f in fills:
        e.on_fill(f)
    e.on_book(book(45.5, 46.5))  # 46
    p = e.snapshot()["traders"]["alice"]["positions"][X]
    assert (p["sod_qty"], p["sod_mark"], p["sod_mark_late"]) == (3, 45.0, False)
    assert p["today"] == {"realized": 2.0, "unrealized": 2.0, "total": 4.0}  # sold 1 at 47; 2 left, 45 -> 46

    e = engine(Clock(now))  # restarted with nothing saved: the first mark stands in, flagged
    assert not e.restore_sod({"day": "2026-11-10", "marks": {X: {"price": 45.0}}})  # yesterday's: not used
    for f in fills:
        e.on_fill(f)
    e.on_book(book(45.5, 46.5))
    p = e.snapshot()["traders"]["alice"]["positions"][X]
    assert (p["sod_mark"], p["sod_mark_late"]) == (46.0, True)
    assert e.sod_state() == {"day": "2026-11-11", "marks": {X: {"price": 46.0, "late": True}}}


def test_a_fill_with_no_time_takes_the_time_it_reached_redis():
    now = utc(2026, 11, 11, 15)
    e = engine(Clock(now))
    e.on_fill(fill("E1", "BUY", 1, 40.0), received=utc(2026, 11, 10, 15))
    assert e.holdings[("alice", X)].sod_qty == 1 and not e.holdings[("alice", X)].today


def test_an_expired_contract_keeps_its_last_mark():
    clock = Clock(utc(2026, 11, 10, 19, 59))  # 14:59 New York
    e = engine(clock)
    e.on_fill(fill("E1", "BUY", 1, 40.0, symbol=EXPIRING))
    e.on_book(book(41.0, 42.0, symbol=EXPIRING))
    clock.now = utc(2026, 11, 10, 20, 0)  # 15:00: trading has ended
    e.on_book(book(10.0, 11.0, symbol=EXPIRING))  # a dead book's quotes are ignored
    p = e.snapshot()["traders"]["alice"]["positions"][EXPIRING]
    assert (p["mark"], p["expired"]) == (41.5, True)


def test_a_contract_closed_out_on_an_earlier_day_is_not_listed_but_still_counts():
    clock = Clock(utc(2026, 11, 10, 15))
    e = engine(clock)
    e.on_fill(fill("E1", "BUY", 1, 40.0))
    e.on_fill(fill("E2", "SELL", 1, 43.0))
    clock.now = utc(2026, 11, 11, 15)
    alice = e.snapshot()["traders"]["alice"]
    assert alice["positions"] == {} and alice["realized"] == 3.0 and alice["today"]["total"] == 0.0


def test_start_of_day_marks_are_saved_to_redis_and_restored():
    async def scenario():
        r = FakeAsyncRedis()
        clock = Clock(utc(2026, 11, 10, 15))
        e = engine(clock)
        e.on_fill(fill("E1", "BUY", 1, 40.0))
        e.on_book(book(42.0, 43.0))
        clock.now = utc(2026, 11, 11, 15)
        task = asyncio.create_task(publish(r, e, every_s=0.02))
        for _ in range(100):
            if await r.get(SOD_KEY):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert json.loads(await r.get(SOD_KEY)) == {"day": "2026-11-11", "marks": {X: {"price": 42.5, "late": False}}}

        restarted = engine(Clock(utc(2026, 11, 11, 16)))
        await load_sod(r, restarted)
        assert restarted.sod_marks[X].price == 42.5

    asyncio.run(asyncio.wait_for(scenario(), 10))
