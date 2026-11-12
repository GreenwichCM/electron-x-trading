"""Which environment a database belongs to (see trading_core.environment): electronx.environment has
one row, written by the first connection after migration 0010. connect() checks it once per process."""

from __future__ import annotations

from trading_core.environment import WrongEnvironment, decide

from .settings import DbSettings

# Tables whose rows mean the database was used already: production does not adopt them.
DATA_TABLES = ("orders", "fills", "exchange_reports", "users")

_checked: set[tuple[str, int, str, str]] = set()


def check_database(conn, settings: DbSettings) -> None:
    """Mark the database as `settings.environment`'s, or raise WrongEnvironment. Does nothing on a
    database migrated to before 0010 (migrate adds the table), and nothing twice in one process."""
    key = (settings.host, settings.port, settings.database, settings.environment)
    if key in _checked:
        return
    cur = conn.cursor()
    try:
        cur.execute("SELECT OBJECT_ID(N'electronx.environment')")
        if cur.fetchone()[0] is None:
            conn.commit()
            return
        cur.execute("SELECT name FROM electronx.environment WITH (UPDLOCK, HOLDLOCK) WHERE id = 1")
        row = cur.fetchone()
        found = row[0] if row else None
        has_data = False
        if found is None and settings.environment == "production":
            counts = " + ".join(f"(SELECT COUNT(*) FROM electronx.{t})" for t in DATA_TABLES)
            cur.execute(f"SELECT {counts}")
            has_data = cur.fetchone()[0] > 0
        what = f"database {settings.database} on {settings.host}"
        if decide(what, settings.environment, found, has_data):
            cur.execute("INSERT electronx.environment (id, name) VALUES (1, %s)", (settings.environment,))
        conn.commit()
    except WrongEnvironment:
        conn.rollback()
        raise
    _checked.add(key)


__all__ = ["WrongEnvironment", "check_database"]
