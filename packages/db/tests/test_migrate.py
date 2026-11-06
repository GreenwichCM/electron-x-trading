import importlib
from contextlib import closing

import pytest
from trading_db import LATEST, DbSettings, MigrationError, batches, connect, load_migrations, migrate, status


def test_scripts_split_on_go_lines():
    sql = "CREATE TABLE a (x int);\nGO\n-- only a comment\ngo\nCREATE VIEW v AS SELECT 1 AS one;\n"
    assert batches(sql) == ["CREATE TABLE a (x int);", "CREATE VIEW v AS SELECT 1 AS one;"]


def test_migrations_are_numbered_from_one():
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, LATEST + 1))


def test_settings_never_show_the_password():
    assert "secret" not in repr(DbSettings(password="secret"))


def test_migrate_creates_the_database_and_runs_each_migration_once(sql_server):
    assert migrate(sql_server) == [m.name for m in load_migrations()]
    assert migrate(sql_server) == []
    applied, pending = status(sql_server)
    assert pending == [] and len(applied) == LATEST
    with closing(connect(sql_server)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sys.objects WHERE type IN ('U', 'V') ORDER BY name")
        names = {row[0] for row in cur.fetchall()}
    assert {"orders", "order_events", "fills", "alerts", "stream_offsets", "trader_fills"} <= names


def test_a_failing_migration_changes_nothing(sql_server, monkeypatch):
    module = importlib.import_module("trading_db.migrate")  # the module, not the function of that name
    broken = module.Migration(1, "0001_broken.sql", "CREATE TABLE dbo.half (x int);\nGO\nSELECT * FROM nowhere;")
    monkeypatch.setattr(module, "load_migrations", lambda: [broken])
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(sql_server)
    with closing(connect(sql_server)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_ID('dbo.half')")
        assert cur.fetchone()[0] is None
