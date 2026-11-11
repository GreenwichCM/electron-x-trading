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


def test_orders_from_the_electronx_screen_and_fills_carry_the_electronx_user():
    screen = {
        "cl_ord_id": "UI-1",
        "order_id": "O9",
        "trader": "",
        "exchange_user": "firms/h/users/dave",
        "source": "electronx",
        "symbol": "BFUT500-ERCOT-HB_NORTH-261231-15",
        "side": "BUY",
        "qty": 2,
        "price": 46.0,
        "tif": "IOC",
        "status": "filled",
        "cum_qty": 2,
        "avg_px": 45.5,
        "created": "20261110-14:03:56.123456789",  # FIX time, to the nanosecond
        "updated": "20261110-14:03:56.123456789",
        "version": 3,
    }
    row = order_row("1-0", screen)
    assert (row["source"], row["exchange_user"], row["trader"]) == ("electronx", "firms/h/users/dave", "")
    assert row["created_at"] == datetime(2026, 11, 10, 14, 3, 56, 123456)
    ours = order_row("2-0", {**screen, "source": None, "exchange_user": None, "trader": "alice"})
    assert (ours["source"], ours["exchange_user"]) == ("app", None)
    fill = {"exec_id": "E1", "symbol": "X", "side": "BUY", "qty": 1, "price": 1.0, "user": "u", "manual": True}
    assert (fill_row("3-0", fill)["exchange_user"], fill_row("3-0", fill)["manual"]) == ("u", True)


def test_the_event_of_an_order_update():
    update = {
        "cl_ord_id": "T1-2",
        "trader": "alice",
        "symbol": "BFUT100-ERCOT-HB_TEST-261110-21",
        "side": "BUY",
        "qty": 1,
        "price": 62.0,
        "status": "filled",
        "leaves": 0,
        "event": "exchange_report",
        "by": "exchange",
        "reason": "",
        "event_time": "2026-11-11T02:00:47.567+00:00",
        "exec_id": "E7",
        "exec_type": "F",
        "exchange_time": "20261111-02:00:49.047894946",
        "last_qty": 1,
        "last_px": 61.25,
        "leaves_qty": 0,
        "trade_id": "TM1",
        "aggressor": True,
        "account": "TA-HURON-00001",
        "capacity": "2",
        "ord_type": "LIMIT",
        "manual": True,
    }
    row = order_row("1-0", update)
    assert (row["event"], row["by_user"], row["exec_type"], row["last_px"], row["trade_id"]) == (
        "exchange_report", "exchange", "F", 61.25, "TM1"
    )  # fmt: skip
    assert row["exchange_time"] == datetime(2026, 11, 11, 2, 0, 49, 47894)
    assert row["event_at"] == datetime(2026, 11, 11, 2, 0, 47, 567000) and row["reason"] is None
    old = order_row("2-0", {k: update[k] for k in ("cl_ord_id", "trader", "symbol", "side", "qty", "price", "status")})
    assert old["event"] is None and old["exec_id"] is None and old["manual"] is None  # from before step 20
