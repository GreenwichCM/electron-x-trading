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


def test_migrations_only_touch_the_electronx_schema():
    for m in load_migrations():
        assert "dbo." not in m.sql.lower(), f"{m.name} refers to dbo"


def test_settings_never_show_the_password():
    assert "secret" not in repr(DbSettings(password="secret"))


def objects(settings, schema):
    with closing(connect(settings)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sys.objects WHERE type IN ('U', 'V') AND schema_id = SCHEMA_ID(%s)", (schema,))
        return {row[0] for row in cur.fetchall()}


def test_migrate_creates_the_database_and_runs_each_migration_once(sql_server):
    assert migrate(sql_server) == [m.name for m in load_migrations()]
    assert migrate(sql_server) == []
    applied, pending = status(sql_server)
    assert pending == [] and len(applied) == LATEST
    tables = {"orders", "order_events", "fills", "alerts", "stream_offsets", "trader_fills", "schema_migrations"}
    assert tables <= objects(sql_server, "electronx")
    assert objects(sql_server, "dbo") == set()


def test_a_shared_database_with_its_own_orders_table(sql_server):
    """Another application's dbo.orders is neither in the way nor touched."""
    from trading_db.migrate import ensure_database

    ensure_database(sql_server)
    with closing(connect(sql_server, autocommit=True)) as conn:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE dbo.orders (id int PRIMARY KEY, customer nvarchar(50)); INSERT dbo.orders VALUES (1, N'x')"
        )
    migrate(sql_server)
    assert objects(sql_server, "dbo") == {"orders"}
    with closing(connect(sql_server)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT customer FROM dbo.orders")
        assert cur.fetchall() == [("x",)]


def test_a_failing_migration_changes_nothing(sql_server, monkeypatch):
    module = importlib.import_module("trading_db.migrate")  # the module, not the function of that name
    sql = "CREATE TABLE electronx.half (x int);\nGO\nSELECT * FROM nowhere;"
    monkeypatch.setattr(module, "load_migrations", lambda: [module.Migration(1, "0001_broken.sql", sql)])
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(sql_server)
    with closing(connect(sql_server)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_ID('electronx.half')")
        assert cur.fetchone()[0] is None
