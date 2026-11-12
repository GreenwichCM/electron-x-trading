"""`python -m trading_db`: errors end in one line, not a traceback."""

import importlib
import sys

import pymssql
import pytest
from pymssql import _mssql
from trading_db import MigrationError

cli = importlib.import_module("trading_db.__main__")


@pytest.mark.parametrize(
    "error",
    [
        MigrationError("0001_history.sql failed and was rolled back"),
        pymssql.OperationalError("CREATE TABLE permission denied"),
        _mssql.MSSQLDriverException("Not connected to any MS SQL server"),
    ],
)
def test_errors_end_in_a_message(monkeypatch, error):
    def fail(settings):
        raise error

    monkeypatch.setenv("DB_PASSWORD", "not-used")
    monkeypatch.setattr(sys, "argv", ["trading_db", "migrate"])
    monkeypatch.setattr(cli, "migrate", fail)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert str(exc.value).startswith("error: ")


def test_check_traders_lists_screen_orders_without_a_trader(sql_server, capsys):
    from contextlib import closing

    from trading_db import connect, migrate, seed_dev_rules

    check_traders = cli.check_traders
    migrate(sql_server)
    seed_dev_rules(sql_server)
    with closing(connect(sql_server)) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT electronx.orders (cl_ord_id, version, trader, symbol, side, qty, price, tif, status, order_id,"
            " cum_qty, avg_px, message, last_stream_id, source, exchange_user) VALUES ('UI-1', 1, N'', 'S', 'BUY',"
            " 1, 45, 'DAY', 'new', 'O1', 0, 0, '', '1-0', 'electronx', 'D@x.com')"
        )
        conn.commit()
    assert not check_traders(sql_server, fill_in=False)
    out = capsys.readouterr().out
    assert "D@x.com  1 orders" in out and "nobody here has this ElectronX login" in out
    with closing(connect(sql_server)) as conn:
        conn.cursor().execute("INSERT electronx.users (user_id, display_name) VALUES (N'd@x.com', N'D')")
        conn.commit()
    assert check_traders(sql_server, fill_in=True)
    out = capsys.readouterr().out
    assert "filled in the trader on 1 orders" in out and "every order from the ElectronX screen has its trader" in out
