"""Connections to SQL Server, through pymssql (FreeTDS, bundled: nothing to install on the machine).

Queries use pymssql's parameter style: `%s` for positional values, `%(name)s` for named ones. Never
build SQL by pasting values into the string; pass them as parameters.
"""

from __future__ import annotations

import pymssql
from pymssql import _mssql

from .settings import DbSettings

# Every error the driver raises, for `except DbError:`. The second kind is pymssql's low-level one,
# raised for example by rollback() on a connection the server has dropped.
DbError = (pymssql.Error, _mssql.MSSQLException)
Connection = pymssql.Connection


def connect(
    settings: DbSettings,
    database: str | None = None,
    autocommit: bool = False,
    login_timeout: int = 10,
    appname: str = "trading",
) -> Connection:
    """A new connection. With `encrypt`, traffic is TLS-encrypted, but the server's certificate is not
    checked yet (fine on one machine; on a network, add certificate checking before going live)."""
    return pymssql.connect(
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
