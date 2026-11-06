"""The recorder against a real SQL Server (skipped without one) and an in-memory Redis."""

import json
import threading
from contextlib import closing

import pytest
from fakeredis import FakeRedis
from trading_db import connect, migrate
from trading_recorder import Recorder, SchemaError

SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


def order(cl, version, status="new", cum_qty=0, trader="alice"):
    return {
        "cl_ord_id": cl,
        "trader": trader,
        "symbol": SYMBOL,
        "side": "BUY",
        "qty": 3,
        "price": 45.25,
        "tif": "GTC",
        "status": status,
        "order_id": "O1" if version > 1 else "",
        "cum_qty": cum_qty,
        "avg_px": 45.25 if cum_qty else 0.0,
        "text": "",
        "created": "2026-11-06T10:00:00.000+00:00",
        "updated": "2026-11-06T10:00:01.000+00:00",
        "version": version,
        "leaves": 3 - cum_qty,
    }


def fill(exec_id, cl, qty):
    return {
        "exec_id": exec_id,
        "order_id": "O1",
        "cl_ord_id": cl,
        "account": "TA-1",
        "symbol": SYMBOL,
        "side": "BUY",
        "qty": qty,
        "price": 45.25,
        "time": "20261106-10:00:01.500",
    }


def add(redis, stream, data):
    return redis.xadd(stream, {"data": json.dumps(data)})


def query(settings, sql):
    with closing(connect(settings)) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()


def drain(recorder):
    total = 0
    while n := recorder.step():
        total += n
    return total


def make_recorder(redis, settings):
    return Recorder(redis, lambda: connect(settings), batch=2, block_ms=10)


def test_refuses_to_start_before_migrate(sql_server):
    from trading_db.migrate import ensure_database

    ensure_database(sql_server)
    with pytest.raises(SchemaError, match="migrate"):
        make_recorder(FakeRedis(), sql_server).step()


def test_records_everything_once_and_keeps_the_latest_order_state(sql_server):
    migrate(sql_server)
    r = FakeRedis()
    add(r, "orders", order("A", 1))
    add(r, "orders", order("A", 3, "partially_filled", cum_qty=1))
    add(r, "orders", order("A", 2))  # an older update, recorded late: history only, not the current state
    add(r, "orders", order("B", 1, trader="dave"))
    add(r, "fills", fill("E1", "A", 1))
    add(r, "alerts", {"active": True, "reason": "position break", "by": "dropcopy", "time": "2026-11-06T10:00:02Z"})
    add(r, "alerts", {"type": "trader_blocked", "trader": "dave", "reason": "loss", "by": "riskmonitor", "time": ""})

    recorder = make_recorder(r, sql_server)
    assert drain(recorder) == 7  # in batches of 2, one transaction each
    assert query(sql_server, "SELECT cl_ord_id, version, status, cum_qty FROM dbo.orders ORDER BY cl_ord_id") == [
        ("A", 3, "partially_filled", 1),
        ("B", 1, "new", 0),
    ]
    assert query(sql_server, "SELECT COUNT(*) FROM dbo.order_events")[0][0] == 4
    assert query(sql_server, "SELECT exec_id, trader, qty FROM dbo.trader_fills") == [("E1", "alice", 1)]
    assert query(sql_server, "SELECT type, trader, by_user FROM dbo.alerts ORDER BY stream_id") == [
        ("killswitch_on", None, "dropcopy"),
        ("trader_blocked", "dave", "riskmonitor"),
    ]

    # restarted: carries on after the last commit, and only new entries are written
    again = make_recorder(r, sql_server)
    assert drain(again) == 0
    add(r, "fills", fill("E2", "A", 2))
    assert drain(again) == 1

    # even reading everything a second time duplicates nothing
    with closing(connect(sql_server, autocommit=True)) as conn:
        conn.cursor().execute("DELETE FROM dbo.stream_offsets")
    assert drain(make_recorder(r, sql_server)) == 8
    assert query(sql_server, "SELECT COUNT(*) FROM dbo.fills")[0][0] == 2
    assert query(sql_server, "SELECT COUNT(*) FROM dbo.order_events")[0][0] == 4


def test_a_bad_entry_is_skipped_not_stuck(sql_server):
    migrate(sql_server)
    r = FakeRedis()
    r.xadd("fills", {"data": json.dumps({"exec_id": "E1"})})  # no symbol, side, qty or price
    add(r, "fills", fill("E2", "A", 1))
    assert drain(make_recorder(r, sql_server)) == 2
    assert query(sql_server, "SELECT exec_id FROM dbo.fills") == [("E2",)]


def test_carries_on_after_sql_server_drops_the_connection(sql_server, sql_server_master):
    migrate(sql_server)
    r = FakeRedis()
    add(r, "orders", order("A", 1))
    recorder = make_recorder(r, sql_server)
    assert drain(recorder) == 1
    cur = recorder.conn.cursor()
    cur.execute("SELECT @@SPID")
    spid = cur.fetchone()[0]
    with closing(connect(sql_server_master, autocommit=True)) as admin:
        admin.cursor().execute(f"KILL {int(spid)}")  # as if the server restarted under the recorder

    add(r, "orders", order("A", 2, "filled", cum_qty=3))
    stop = threading.Event()
    thread = threading.Thread(target=recorder.run, args=(stop,))
    thread.start()
    for _ in range(300):
        if recorder.recorded == 2:
            break
        stop.wait(0.02)
    stop.set()
    thread.join(10)
    assert not thread.is_alive() and recorder.recorded == 2
    assert query(sql_server, "SELECT status FROM dbo.orders") == [("filled",)]


def test_run_stops_when_asked(sql_server):
    migrate(sql_server)
    r = FakeRedis()
    add(r, "orders", order("A", 1))
    recorder = make_recorder(r, sql_server)
    stop = threading.Event()
    thread = threading.Thread(target=recorder.run, args=(stop,))
    thread.start()
    for _ in range(200):
        if recorder.recorded:
            break
        stop.wait(0.02)
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and recorder.recorded == 1
