"""Connections to SQL Server, through pymssql (FreeTDS, bundled: nothing to install on the machine).

Queries use pymssql's parameter style: `%s` for positional values, `%(name)s` for named ones. Never
build SQL by pasting values into the string; pass them as parameters.
"""

from __future__ import annotations

import pymssql
from pymssql import _mssql

from .environment import check_database
from .settings import DbSettings

# Every error the driver raises. The second kind is pymssql's low-level one, raised for example by
# rollback() on a connection the server has dropped. It is a tuple: write `except DbError:`, or
# `except (OtherError, *DbError):` alongside other exceptions (a tuple inside a tuple is an error).
DbError = (pymssql.Error, _mssql.MSSQLException)
Connection = pymssql.Connection


def connect(
    settings: DbSettings,
    database: str | None = None,
    autocommit: bool = False,
    login_timeout: int = 10,
    appname: str = "trading",
    check_environment: bool = True,
) -> Connection:
    """A new connection. With `encrypt`, traffic is TLS-encrypted, but the server's certificate is not
    checked yet (fine on one machine; on a network, add certificate checking before going live).

    The first connection to the database in this process also checks that it belongs to this
    environment (TRADING_ENV), and marks it if nobody has: WrongEnvironment if it is another's."""
    conn = pymssql.connect(
        server=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        database=database or settings.database,
        autocommit=autocommit,
        login_timeout=login_timeout,
        appname=appname,
        tds_version="7.4",
        encryption="require" if settings.encrypt else "off",
    )
    if check_environment and database in (None, settings.database):
        try:
            check_database(conn, settings)
        except BaseException:
            conn.close()
            raise
    return conn
