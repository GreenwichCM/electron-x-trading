"""Test fixtures shared by every package and service.

`sql_server` gives a test its own new, empty database on the development SQL Server
(`docker compose up -d sqlserver`) and drops it afterwards. The sa password comes from
MSSQL_SA_PASSWORD, or from the .env next to compose.yaml. Without a server, those tests are skipped.
"""

import os
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from trading_db import DbError, DbSettings, connect


def _sa_password() -> str:
    if os.environ.get("MSSQL_SA_PASSWORD"):
        return os.environ["MSSQL_SA_PASSWORD"]
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("MSSQL_SA_PASSWORD="):
                return line.split("=", 1)[1].strip()
    return ""


@pytest.fixture(scope="session")
def sql_server_master() -> DbSettings:
    password = _sa_password()
    if not password:
        pytest.skip("no MSSQL_SA_PASSWORD (set it in .env and run `docker compose up -d sqlserver`)")
    settings = DbSettings(
        host=os.environ.get("TEST_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TEST_DB_PORT", "1433")),
        user="sa",
        password=password,
        database="master",
    )
    try:
        connect(settings, autocommit=True, login_timeout=3).close()
    except DbError as exc:
        pytest.skip(f"SQL Server is not reachable at {settings.host}:{settings.port} ({exc})")
    return settings


@pytest.fixture
def sql_server(sql_server_master: DbSettings):
    """Settings for a database that does not exist yet; migrate() creates it."""
    name = f"trading_test_{uuid.uuid4().hex[:10]}"
    yield replace(sql_server_master, database=name)
    with closing(connect(sql_server_master, autocommit=True)) as conn:
        conn.cursor().execute(
            f"IF DB_ID('{name}') IS NOT NULL BEGIN "
            f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}]; END"
        )
