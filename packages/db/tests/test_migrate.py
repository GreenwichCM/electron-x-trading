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
    tables = {"orders", "order_events", "fills", "alerts", "stream_offsets", "schema_migrations"}
    tables |= {"users", "user_roles", "accounts", "account_access", "entitlements", "limits", "rule_changes"}
    tables |= {"exchange_reports"}
    assert objects(sql_server, "electronx") == tables
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


def test_traders_become_users_with_their_electronx_login(sql_server):
    """Migration 0009 on a database that had traders: a trader with an ElectronX login keeps it as their id,
    the test users get @example.test ones, and every old id in the rules and the history follows."""
    settings = sql_server
    with closing(connect(settings, database="master", autocommit=True)) as conn:
        conn.cursor().execute(f"CREATE DATABASE [{settings.database}]")
    with closing(connect(settings)) as conn:
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA electronx")
        cur.execute(
            "CREATE TABLE electronx.schema_migrations (version int PRIMARY KEY, name nvarchar(200) NOT NULL,"
            " applied_at datetime2(3) NOT NULL DEFAULT sysutcdatetime())"
        )
        for m in load_migrations():
            if m.version >= 9:
                break
            for batch in batches(m.sql):
                cur.execute(batch)
            cur.execute("INSERT electronx.schema_migrations (version, name) VALUES (%s, %s)", (m.version, m.name))
        cur.execute(
            "INSERT electronx.traders (trader_id, exchange_trader_id, active) VALUES"
            " (N'alice', 'Jane@Example.com', 1), (N'dave', NULL, 0)"
        )
        cur.execute("INSERT electronx.limits (trader_id, max_position) VALUES (N'alice', 5)")
        cur.execute(
            "INSERT electronx.orders (cl_ord_id, version, trader, symbol, side, qty, price, tif, status, order_id,"
            " cum_qty, avg_px, message, last_stream_id, account) VALUES ('C1', 1, N'alice', 'S', 'BUY', 1, 45,"
            " 'DAY', 'new', 'O1', 0, 0, '', '1-0', 'TA-1')"
        )
        cur.execute(
            "INSERT electronx.rule_changes (kind, action, trader_id, market, product, reason, requested_by)"
            " VALUES ('trader', 'set', N'dave', '*', '*', 'r', N'bob')"
        )
        conn.commit()
    assert migrate(settings)[0].startswith("0009")

    def rows(sql):
        with closing(connect(settings)) as conn:
            cur = conn.cursor()
            cur.execute(sql)
            return cur.fetchall()

    assert rows("SELECT user_id, display_name, active FROM electronx.users ORDER BY user_id") == [
        ("bob@example.test", "Bob (risk)", True),
        ("carol@example.test", "Carol (management)", True),
        ("dave@example.test", "Dave (trader)", False),
        ("erin@example.test", "Erin (risk)", True),
        ("jane@example.com", "Jane", True),
    ]
    assert rows("SELECT user_id, role FROM electronx.user_roles WHERE role = 'trader' ORDER BY user_id") == [
        ("dave@example.test", "trader"),
        ("jane@example.com", "trader"),
    ]
    assert rows("SELECT trader FROM electronx.orders") == [("jane@example.com",)]
    assert rows("SELECT trader_id FROM electronx.limits") == [("jane@example.com",)]
    assert rows("SELECT account_id FROM electronx.accounts") == [("TA-1",)]
    assert rows("SELECT user_id FROM electronx.account_access ORDER BY user_id") == [
        ("dave@example.test",),
        ("jane@example.com",),
    ]
    assert rows("SELECT trader_id, requested_by, status FROM electronx.rule_changes") == [
        ("dave@example.test", "bob@example.test", "stale")
    ]
