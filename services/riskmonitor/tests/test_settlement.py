"""Expiry times from the exchange, and closing positions at the settlement price."""

from datetime import UTC, datetime

from trading_core import TradingRules
from trading_riskmonitor import RiskEngine

X = "BFUT500-ERCOT-HB_NORTH-261110-15"  # by its symbol, trading ends 15:00 Central = 21:00 UTC


def utc(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def book(bid, ask, state="OPEN", settlement=None):
    return {"symbol": X, "bid": bid, "ask": ask, "stale": False, "last_trade": None, "state": state,
            "settlement": settlement}  # fmt: skip


def engine(clock, **limits):
    e = RiskEngine(TradingRules.everyone(["alice"], **limits) if limits else None, clock=clock)
    e.on_order({"cl_ord_id": "A1", "trader": "alice"})
    e.on_fill({"exec_id": "E1", "cl_ord_id": "A1", "symbol": X, "side": "BUY", "qty": 2, "price": 40.0})
    e.on_book(book(41.0, 42.0))  # 41.5
    return e


def test_trading_ends_at_the_symbols_hour_in_the_iso_time_zone():
    clock = Clock(utc(2026, 11, 10, 20, 59))
    e = engine(clock)
    assert not e.expired(X, clock.now)
    clock.now = utc(2026, 11, 10, 21)  # 15:00 Central
    assert e.expired(X, clock.now)


def test_the_exchanges_last_trade_time_comes_first():
    clock = Clock(utc(2026, 11, 10, 20, 15))
    e = engine(clock)
    e.on_instrument({"symbol": X, "last_trade_time": iso(utc(2026, 11, 10, 20, 15))})  # 45 minutes early
    assert e.expired(X, clock.now)
    e.on_book(book(10.0, 11.0))  # quotes after the last trade are ignored
    assert e.snapshot()["traders"]["alice"]["positions"][X]["mark"] == 41.5


def test_market_data_state_expired_counts_too():
    clock = Clock(utc(2026, 11, 10, 18))
    e = engine(clock)
    e.on_book(book(None, None, state="EXPIRED"))
    p = e.snapshot()["traders"]["alice"]["positions"][X]
    assert p["expired"] and not p["settled"] and p["mark"] == 41.5


def test_a_final_settlement_closes_the_position_at_that_price():
    clock = Clock(utc(2026, 11, 10, 22))
    e = engine(clock, max_position=4, max_loss=1000)
    settlement = {"price": 44.25, "final": True, "flag": "", "time": iso(utc(2026, 11, 10, 21, 30))}
    e.on_book(book(None, None, state="EXPIRED", settlement=settlement))
    snap = e.snapshot()
    alice = snap["traders"]["alice"]
    p = alice["positions"][X]
    assert (p["settled"], p["mark"], p["mark_source"]) == (True, 44.25, "settlement")
    assert p["realized"] == 8.5 and p["unrealized"] == 0.0  # 2 x (44.25 - 40)
    assert alice["today"] == {"realized": 8.5, "unrealized": 0.0, "total": 8.5}
    assert alice["limits"]["position"]["used"] == 0 and snap["desk"]["positions"] == {}  # no longer held

    clock.now = utc(2026, 11, 12, 15)  # two days on: no longer listed, still in the totals
    alice = e.snapshot()["traders"]["alice"]
    assert alice["positions"] == {} and alice["realized"] == 8.5 and alice["today"]["total"] == 0.0


def test_an_indicative_settlement_is_only_a_mark():
    clock = Clock(utc(2026, 11, 10, 22))
    e = engine(clock)
    e.on_book(book(None, None, state="EXPIRED", settlement={"price": 43.0, "final": False, "flag": "3"}))
    p = e.snapshot()["traders"]["alice"]["positions"][X]
    assert (p["settled"], p["mark"], p["mark_source"]) == (False, 43.0, "indicative settlement")
    assert p["unrealized"] == 6.0 and p["realized"] == 0.0
