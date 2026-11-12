"""The database's environment mark, and adding the first users of a new database (skipped without SQL
Server)."""

import importlib
import sys
from contextlib import closing
from dataclasses import replace

import pytest
from trading_db import WrongEnvironment, add_user, connect, migrate, seed_dev_rules
from trading_db.changes import ChangeError

cli = importlib.import_module("trading_db.__main__")


def marked(settings):
    with closing(connect(settings)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM electronx.environment")
        return [r[0] for r in cur.fetchall()]


def test_the_first_environment_to_connect_marks_the_database(sql_server):
    beta = replace(sql_server, environment="beta")
    migrate(beta)
    assert marked(beta) == ["beta"]
    with pytest.raises(WrongEnvironment, match="belongs to beta, but this is development"):
        connect(sql_server)
    with pytest.raises(WrongEnvironment, match="belongs to beta, but this is production"):
        connect(replace(sql_server, environment="production"))


def test_production_refuses_a_database_with_data_from_before(sql_server):
    migrate(sql_server)
    seed_dev_rules(sql_server)  # test users, and the mark "development"
    with closing(connect(sql_server)) as conn:
        conn.cursor().execute("DELETE electronx.environment")  # as if from before migration 0010
        conn.commit()
    with pytest.raises(WrongEnvironment, match="already holds data"):
        connect(replace(sql_server, environment="production"))


def test_setting_up_production_adds_the_first_users_directly(sql_server):
    prod = replace(sql_server, environment="production")
    migrate(prod)
    with closing(connect(prod)) as conn:
        assert add_user(conn, "Jane@Example.com", ["trader", "risk"], ["TA-HURON-00001"]) == ["TA-HURON-00001"]
        assert add_user(conn, "joe@example.com", ["risk"], [], name="Joe") == []
        with pytest.raises(ChangeError, match="has its risk users already"):
            add_user(conn, "sam@example.com", ["trader"], ["TA-HURON-00001"])
        cur = conn.cursor()
        cur.execute("SELECT user_id, display_name FROM electronx.users ORDER BY user_id")
        assert cur.fetchall() == [("jane@example.com", "jane"), ("joe@example.com", "Joe")]
        cur.execute("SELECT user_id, account_id FROM electronx.account_access")
        assert cur.fetchall() == [("jane@example.com", "TA-HURON-00001")]
        cur.execute("SELECT kind, trader_id, status, requested_by, decided_by FROM electronx.rule_changes")
        assert sorted(cur.fetchall()) == [
            ("user", "jane@example.com", "approved", "setup", "setup"),
            ("user", "joe@example.com", "approved", "setup", "setup"),
        ]
    assert marked(prod) == ["production"]
    with pytest.raises(WrongEnvironment, match="belongs to production"):
        connect(replace(sql_server, environment="beta"))


def test_add_user_refuses_someone_already_there(sql_server):
    migrate(sql_server)
    with closing(connect(sql_server)) as conn:
        add_user(conn, "jane@example.com", ["trader"], ["TA-HURON-00001"])
        with pytest.raises(ChangeError, match="is a user already"):
            add_user(conn, "jane@example.com", ["risk"], [])
        with pytest.raises(ChangeError, match="roles are"):
            add_user(conn, "joe@example.com", ["boss"], [])


def test_add_user_command(sql_server, monkeypatch, capsys):
    migrate(sql_server)
    env = {
        "DB_HOST": sql_server.host,
        "DB_PORT": str(sql_server.port),
        "DB_NAME": sql_server.database,
        "DB_USER": sql_server.user,
        "DB_PASSWORD": sql_server.password,
        "DB_ENCRYPT": "Y" if sql_server.encrypt else "N",
        "TRADING_ENV": "development",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    args = ["trading_db", "add-user", "jane@example.com", "--roles", "trader,risk", "--accounts", "TA-1"]
    monkeypatch.setattr(sys, "argv", args)
    cli.main()
    out = capsys.readouterr().out
    assert "added jane@example.com (risk, trader)" in out and "TA-1" in out
    monkeypatch.setattr(sys, "argv", ["trading_db", "add-user", "jane@example.com"])
    with pytest.raises(SystemExit, match="needs the email and --roles"):
        cli.main()


def test_clear_data_empties_a_test_database_so_production_can_start(sql_server, monkeypatch, capsys):
    migrate(sql_server)
    seed_dev_rules(sql_server)  # marks it development, with users and rules
    env = {
        "DB_HOST": sql_server.host,
        "DB_PORT": str(sql_server.port),
        "DB_NAME": sql_server.database,
        "DB_USER": sql_server.user,
        "DB_PASSWORD": sql_server.password,
        "DB_ENCRYPT": "Y" if sql_server.encrypt else "N",
        "TRADING_ENV": "production",  # the production settings can clear it: the check is skipped
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["trading_db", "clear-data"])
    cli.main()
    out = capsys.readouterr().out
    assert "marked as development's" in out and "electronx.users: 5 rows" in out and "nothing deleted" in out
    monkeypatch.setattr(sys, "argv", ["trading_db", "clear-data", "--confirm", sql_server.database])
    cli.main()
    assert "the tables are empty" in capsys.readouterr().out
    prod = replace(sql_server, environment="production")
    with closing(connect(prod)) as conn:  # empty now: production marks it
        add_user(conn, "jane@example.com", ["risk"], [])
    monkeypatch.setattr(sys, "argv", ["trading_db", "clear-data", "--confirm", sql_server.database])
    with pytest.raises(SystemExit, match="production's"):
        cli.main()
    assert marked(prod) == ["production"]
