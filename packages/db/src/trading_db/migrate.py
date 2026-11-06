"""Schema migrations: numbered .sql files in trading_db/migrations, applied in order, each once.

A migration is plain T-SQL, split into batches on lines that say only GO (like sqlcmd and SSMS), and
runs in one transaction with its row in dbo.schema_migrations, so it is applied completely or not at
all. Never edit a migration that has been applied anywhere: add the next number instead.
"""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass
from importlib import resources

from .connection import Connection, DbError, connect
from .settings import DbSettings

log = logging.getLogger("db.migrate")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    folder = resources.files("trading_db") / "migrations"
    found = []
    for f in sorted(folder.iterdir(), key=lambda p: p.name):
        if f.name.endswith(".sql"):
            found.append(Migration(int(f.name.split("_", 1)[0]), f.name, f.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migrations must be numbered 0001, 0002, ... without gaps: {versions}")
    return found


LATEST = len(load_migrations())


def batches(sql: str) -> list[str]:
    """Split a script on lines that are just GO."""
    out: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(current))
            current = []
        else:
            current.append(line)
    out.append("\n".join(current))
    return [b for b in out if _has_sql(b)]


def _has_sql(batch: str) -> bool:
    """False for a batch of only blank lines and -- comments."""
    return any(line.strip() and not line.strip().startswith("--") for line in batch.splitlines())


def ensure_database(settings: DbSettings) -> None:
    """Create the database if it does not exist yet (development; in production a DBA does this)."""
    with closing(connect(settings, database="master", autocommit=True)) as conn:
        cur = conn.cursor()
        cur.execute(
            "DECLARE @sql nvarchar(400) = N'CREATE DATABASE ' + QUOTENAME(%s); "
            "IF DB_ID(%s) IS NULL EXEC sp_executesql @sql;",
            (settings.database, settings.database),
        )


def _ensure_table(conn: Connection) -> None:
    conn.cursor().execute(
        "IF OBJECT_ID('dbo.schema_migrations') IS NULL "
        "CREATE TABLE dbo.schema_migrations ("
        " version int NOT NULL PRIMARY KEY,"
        " name nvarchar(200) NOT NULL,"
        " applied_at datetime2(3) NOT NULL DEFAULT sysutcdatetime())"
    )
    conn.commit()


def applied_versions(conn: Connection) -> set[int]:
    cur = conn.cursor()
    cur.execute("SELECT CASE WHEN OBJECT_ID('dbo.schema_migrations') IS NULL THEN 0 ELSE 1 END")
    if not cur.fetchone()[0]:
        return set()
    cur.execute("SELECT version FROM dbo.schema_migrations")
    return {row[0] for row in cur.fetchall()}


def schema_version(conn: Connection) -> int:
    return max(applied_versions(conn), default=0)


def migrate(settings: DbSettings) -> list[str]:
    """Apply every migration not applied yet; returns their names."""
    ensure_database(settings)
    done = []
    with closing(connect(settings)) as conn:
        _ensure_table(conn)
        applied = applied_versions(conn)
        for m in load_migrations():
            if m.version in applied:
                continue
            cur = conn.cursor()
            try:
                for batch in batches(m.sql):
                    cur.execute(batch)
                cur.execute("INSERT dbo.schema_migrations (version, name) VALUES (%s, %s)", (m.version, m.name))
                conn.commit()
            except DbError as exc:
                conn.rollback()
                raise MigrationError(f"{m.name} failed and was rolled back: {exc}") from exc
            log.info("applied %s", m.name)
            done.append(m.name)
    return done


def status(settings: DbSettings) -> tuple[list[str], list[str]]:
    """(applied, pending) migration names."""
    with closing(connect(settings)) as conn:
        applied = applied_versions(conn)
    all_ = load_migrations()
    return [m.name for m in all_ if m.version in applied], [m.name for m in all_ if m.version not in applied]
