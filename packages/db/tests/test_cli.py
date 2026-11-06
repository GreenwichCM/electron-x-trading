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
