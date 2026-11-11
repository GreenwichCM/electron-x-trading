"""The recorder against a real SQL Server (skipped without one) and an in-memory Redis."""

import json
import threading
from contextlib import closing
from datetime import datetime

import pytest
from fakeredis import FakeRedis
from trading_db import connect, migrate, reconcile
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
    assert query(sql_server, "SELECT cl_ord_id, version, status, cum_qty FROM electronx.orders ORDER BY cl_ord_id") == [
        ("A", 3, "partially_filled", 1),
        ("B", 1, "new", 0),
    ]
    assert query(sql_server, "SELECT COUNT(*) FROM electronx.order_events")[0][0] == 4
    fills_with_trader = (
        "SELECT f.exec_id, o.trader, f.qty FROM electronx.fills AS f "
        "LEFT JOIN electronx.orders AS o ON o.cl_ord_id = f.cl_ord_id"
    )
    assert query(sql_server, fills_with_trader) == [("E1", "alice", 1)]
    assert query(sql_server, "SELECT type, trader, by_user FROM electronx.alerts ORDER BY stream_id") == [
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
        conn.cursor().execute("DELETE FROM electronx.stream_offsets")
    assert drain(make_recorder(r, sql_server)) == 8
    assert query(sql_server, "SELECT COUNT(*) FROM electronx.fills")[0][0] == 2
    assert query(sql_server, "SELECT COUNT(*) FROM electronx.order_events")[0][0] == 4


def test_a_bad_entry_is_skipped_not_stuck(sql_server):
    migrate(sql_server)
    r = FakeRedis()
    r.xadd("fills", {"data": json.dumps({"exec_id": "E1"})})  # no symbol, side, qty or price
    add(r, "fills", fill("E2", "A", 1))
    assert drain(make_recorder(r, sql_server)) == 2
    assert query(sql_server, "SELECT exec_id FROM electronx.fills") == [("E2",)]


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
    assert query(sql_server, "SELECT status FROM electronx.orders") == [("filled",)]


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


def test_orders_from_the_electronx_screen_are_recorded_with_their_trader(sql_server):
    migrate(sql_server)
    with closing(connect(sql_server, autocommit=True)) as conn:
        conn.cursor().execute(
            "INSERT electronx.traders (trader_id, exchange_trader_id) VALUES (N'dave', 'firms/h/users/dave')"
        )
    r = FakeRedis()
    screen = {
        **order("UI-1", 1, "filled", cum_qty=3, trader=""),
        "order_id": "O9",
        "source": "electronx",
        "exchange_user": "firms/h/users/dave",
    }
    add(r, "screen_orders", screen)
    add(r, "screen_orders", {**screen, "cl_ord_id": "UI-2", "exchange_user": "firms/h/users/nobody"})
    add(r, "fills", {**fill("E1", "UI-1", 3), "order_id": "O9", "user": "firms/h/users/dave", "manual": True})
    add(r, "orders", order("A", 1))
    assert drain(make_recorder(r, sql_server)) == 4
    rows = "SELECT cl_ord_id, trader, source, exchange_user FROM electronx.orders ORDER BY cl_ord_id"
    assert query(sql_server, rows) == [
        ("A", "alice", "app", None),
        ("UI-1", "dave", "electronx", "firms/h/users/dave"),
        ("UI-2", "", "electronx", "firms/h/users/nobody"),  # no trader has that ElectronX id
    ]
    assert query(sql_server, "SELECT exchange_user, manual FROM electronx.fills") == [("firms/h/users/dave", True)]


def test_the_whole_event_is_recorded(sql_server):
    migrate(sql_server)
    r = FakeRedis()
    sent = {"account": "TA-1", "capacity": "2", "ord_type": "LIMIT", "manual": True, "exchange_user": "a@x.com"}
    add(
        r,
        "orders",
        {**order("A", 1), **sent, "event": "new_order", "by": "alice", "event_time": "2026-11-11T02:00:40Z"},
    )
    ack = {"event": "exchange_report", "by": "exchange", "exec_id": "X1", "exec_type": "0", "leaves_qty": 3}
    add(r, "orders", {**order("A", 2), **sent, **ack, "exchange_time": "20261111-02:00:40.394123456"})
    refused = {"event": "cancel_refused", "by": "dave", "reason": "not your order"}
    add(r, "orders", {**order("A", 3), **sent, **refused})
    trade = {"trade_id": "TM1", "aggressor": False, "commission": 0.5, "leaves_qty": 2, "capacity": "2"}
    add(r, "fills", {**fill("E1", "A", 1), **trade})
    assert drain(make_recorder(r, sql_server)) == 4
    rows = query(
        sql_server,
        "SELECT version, event, by_user, reason, exec_id, exec_type, leaves_qty, account, manual "
        "FROM electronx.order_events ORDER BY version",
    )
    assert rows == [
        (1, "new_order", "alice", None, None, None, 3, "TA-1", True),
        (2, "exchange_report", "exchange", None, "X1", "0", 3, "TA-1", True),
        (3, "cancel_refused", "dave", "not your order", None, None, 3, "TA-1", True),
    ]
    exchange_time = query(sql_server, "SELECT exchange_time FROM electronx.order_events WHERE version = 2")[0][0]
    assert exchange_time.isoformat() == "2026-11-11T02:00:40.394123"
    assert query(sql_server, "SELECT last_event, leaves_qty, account, exchange_user FROM electronx.orders") == [
        ("cancel_refused", 3, "TA-1", "a@x.com")
    ]
    assert query(sql_server, "SELECT trade_id, aggressor, commission, leaves_qty FROM electronx.fills") == [
        ("TM1", False, 0.5, 2)
    ]


def test_reconcile_finds_the_reports_only_one_session_saw(sql_server):
    migrate(sql_server)
    r = FakeRedis()
    at = "2026-11-11T02:00:40Z"

    def event(version, exec_id, exec_type, cum_qty, status):
        update = {**order("A", version, status, cum_qty=cum_qty), "order_id": "O9", "event_time": at}
        return {**update, "event": "exchange_report", "by": "exchange", "exec_id": exec_id, "exec_type": exec_type}

    def report(exec_id, exec_type, cum_qty, order_id="O9", cl_ord_id="A"):
        return {
            "exec_id": exec_id,
            "order_id": order_id,
            "cl_ord_id": cl_ord_id,
            "symbol": SYMBOL,
            "side": "BUY",
            "exec_type": exec_type,
            "ord_status": "1",
            "cum_qty": cum_qty,
            "received": at,
            "raw": f"35=8|17={exec_id}",
        }

    add(r, "orders", {**order("A", 1), "event": "new_order", "by": "alice", "event_time": at})
    add(r, "orders", event(2, "X1", "0", 0, "new"))  # on both sessions, same ExecID
    add(r, "orders", event(3, "Y2", "F", 1, "partially_filled"))  # on both; the drop copy numbers it differently
    add(r, "orders", event(4, "X4", "F", 2, "partially_filled"))  # the drop copy never had this one
    add(r, "exchange_reports", report("X1", "0", 0))
    add(r, "exchange_reports", report("D2", "F", 1))
    add(r, "exchange_reports", report("X5", "F", 3))  # orderentry never recorded this one
    add(r, "exchange_reports", report("S1", "0", 0, order_id="O77", cl_ord_id="UI-1"))  # from the screen: not compared
    assert drain(make_recorder(r, sql_server)) == 8
    assert query(sql_server, "SELECT raw FROM electronx.exchange_reports WHERE exec_id = 'X1'") == [("35=8|17=X1",)]

    with closing(connect(sql_server)) as conn:
        result = reconcile(conn, since=datetime(2026, 11, 11), until=datetime(2026, 11, 12))
    assert (result.drop_copy_reports, result.orderentry_reports) == (3, 3)
    assert [row[2] for row in result.drop_copy_only] == ["X5"]
    assert [row[2] for row in result.orderentry_only] == ["X4"]
    assert not result.ok
