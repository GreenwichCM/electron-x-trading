from datetime import datetime

from trading_recorder import alert_row, fill_row, order_row, parse_time


def test_times_become_naive_utc():
    assert parse_time("20261106-02:56:02.069") == datetime(2026, 11, 6, 2, 56, 2, 69000)
    assert parse_time("2026-11-06T03:56:02.069+01:00") == datetime(2026, 11, 6, 2, 56, 2, 69000)
    assert parse_time("") is None and parse_time("not a time") is None


def test_orders_from_before_versions_existed():
    o = {"cl_ord_id": "A", "trader": "alice", "symbol": "X", "side": "BUY", "qty": 2, "price": 40.5, "status": "new"}
    row = order_row("1-0", o)
    assert row["version"] == 0 and row["message"] == "" and row["created_at"] is None


def test_fill_and_alert_rows():
    f = {"exec_id": "E1", "symbol": "X", "side": "SELL", "qty": 1, "price": 41.25, "time": "20261106-02:56:02.069"}
    assert fill_row("2-0", f)["exec_time"] == datetime(2026, 11, 6, 2, 56, 2, 69000)
    on = alert_row("3-0", {"active": True, "reason": "break", "by": "dropcopy", "time": ""}, "{}")
    blocked = alert_row("4-0", {"type": "trader_blocked", "trader": "alice", "by": "riskmonitor"}, "{}")
    assert (on["type"], on["trader"]) == ("killswitch_on", None)
    assert (blocked["type"], blocked["trader"], blocked["reason"]) == ("trader_blocked", "alice", "")
