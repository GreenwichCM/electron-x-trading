"""Proposing and approving rule changes (four eyes). Validation runs without SQL Server; the rest is
skipped without one."""

from contextlib import closing

import pytest
from trading_db import connect, load_rules, migrate, seed_dev_rules
from trading_db.attribution import fill_in_traders, unknown_users
from trading_db.changes import (
    ChangeError,
    ProposedChange,
    approve,
    current_rules,
    list_changes,
    propose,
    reject,
    validate,
    withdraw,
)

PJM = "BFUT100-PJM-WESTERN_HUB-261231-15"


def limit(trader="alice@example.test", market="*", product="*", reason="r", action="set", **values):
    return ProposedChange("limit", action, trader, market, product, values, reason)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (limit(max_order_qty=5, reason=" "), "reason"),
        (limit(market="NOWHERE", max_order_qty=5), "market"),
        (limit(product="GAS", max_order_qty=5), "product"),
        (limit(max_order_qty=2.5), "whole number"),
        (limit(max_position=-1), "negative"),
        (limit(market="PJM", max_loss=100), "per trader"),
        (limit(), "at least one"),
        (ProposedChange("user", "set", "*", reason="r"), "their own id"),
        (ProposedChange("user", "set", "frank", reason="r"), "company email"),
        (ProposedChange("user", "set", "f@x.com", values={"roles": "trader, boss"}, reason="r"), "roles are"),
        (ProposedChange("entitlement", "set", "alice@example.test", "PJM", "*", {"x": 1}, "r"), "no values"),
    ],
)
def test_bad_changes_are_refused_with_a_reason(change, message):
    with pytest.raises(ChangeError, match=message) as exc:
        validate(change)
    assert exc.value.status == 400


def test_values_are_tidied():
    tidy = validate(limit(market="pjm", product="bfut100", max_position="5", max_order_qty=""))
    assert (tidy.market, tidy.product) == ("PJM", "BFUT100")
    assert tidy.values["max_position"] == 5 and tidy.values["max_order_qty"] is None


@pytest.fixture
def db(sql_server):
    migrate(sql_server)
    seed_dev_rules(sql_server)
    with closing(connect(sql_server)) as conn:
        yield conn


def statuses(conn):
    return {c["change_id"]: c["status"] for c in list_changes(conn)}


def test_a_change_needs_a_second_person(db):
    change_id = propose(
        db,
        "bob@example.test",
        ProposedChange("entitlement", "set", "alice@example.test", "PJM", "*", reason="alice covers PJM"),
    )
    assert "not entitled" in load_rules(db).check_trader("alice@example.test", PJM)  # nothing changes until approved
    with pytest.raises(ChangeError, match="second person") as exc:
        approve(db, change_id, "bob@example.test")
    assert exc.value.status == 403
    approve(db, change_id, "erin@example.test")
    assert load_rules(db).check_trader("alice@example.test", PJM) is None
    (change,) = list_changes(db)
    assert (change["status"], change["requested_by"], change["decided_by"]) == (
        "approved",
        "bob@example.test",
        "erin@example.test",
    )
    assert (change["before"], change["after"]) == (None, {})
    with pytest.raises(ChangeError, match="already approved"):
        approve(db, change_id, "erin@example.test")


def test_limits_keep_before_and_after(db):
    change_id = propose(
        db, "bob@example.test", limit("dave@example.test", "PJM", "BFUT100", max_position=5, reason="new trader")
    )
    approve(db, change_id, "erin@example.test")
    assert load_rules(db).limits_for("dave@example.test", PJM).max_position == 5
    change_id = propose(
        db,
        "erin@example.test",
        limit("dave@example.test", "PJM", "BFUT100", max_position=8, max_order_qty=2, reason="more"),
    )
    approve(db, change_id, "bob@example.test")
    rules = load_rules(db).limits_for("dave@example.test", PJM)
    assert (rules.max_position, rules.max_order_qty) == (8, 2)
    latest = list_changes(db)[0]
    assert latest["before"]["max_position"] == 5 and latest["after"]["max_position"] == 8
    with pytest.raises(ChangeError, match="already the rule"):
        propose(db, "bob@example.test", limit("dave@example.test", "PJM", "BFUT100", max_position=8, max_order_qty=2))


def test_a_proposal_overtaken_by_another_change_goes_stale(db):
    first = propose(
        db,
        "bob@example.test",
        ProposedChange("user", "set", "dave@example.test", values={"active": False}, reason="leave"),
    )
    second = propose(
        db,
        "erin@example.test",
        ProposedChange("user", "set", "dave@example.test", values={"note": "PJM desk"}, reason="note"),
    )
    approve(db, first, "erin@example.test")
    with pytest.raises(ChangeError, match="changed after") as exc:
        approve(db, second, "bob@example.test")  # would quietly make dave active again
    assert exc.value.status == 409
    assert statuses(db) == {first: "approved", second: "stale"}
    assert load_rules(db).check_trader("dave@example.test", PJM) == "dave@example.test is not active"


def test_reject_withdraw_and_remove(db):
    a = propose(
        db, "bob@example.test", ProposedChange("user", "set", "frank@x.com", values={"roles": ["trader"]}, reason="new")
    )
    b = propose(
        db,
        "bob@example.test",
        ProposedChange("entitlement", "delete", "alice@example.test", "ERCOT", "*", reason="moves desk"),
    )
    c = propose(
        db,
        "bob@example.test",
        limit(
            "*",
            max_order_qty=5,
            max_order_notional=5000,
            max_open_orders=20,
            max_position=50,
            max_loss=5000,
            reason="smaller orders",
        ),
    )
    with pytest.raises(ChangeError, match="withdraw your own"):
        reject(db, a, "bob@example.test")
    reject(db, a, "erin@example.test", "not hired yet")
    with pytest.raises(ChangeError, match="only whoever proposed"):
        withdraw(db, b, "erin@example.test")
    withdraw(db, b, "bob@example.test")
    approve(db, c, "erin@example.test")
    assert statuses(db) == {a: "rejected", b: "withdrawn", c: "approved"}
    rules = current_rules(db)
    assert [u["user_id"] for u in rules["users"]][0] == "alice@example.test" and len(rules["users"]) == 5
    assert {"trader_id": "alice@example.test", "market": "ERCOT", "product": "*"} in rules["entitlements"]
    desk = next(r for r in rules["limits"] if r["trader_id"] == "*")
    assert desk["max_order_qty"] == 5 and desk["max_loss"] == 5000.0
    with pytest.raises(ChangeError, match="no such rule"):
        propose(
            db,
            "bob@example.test",
            ProposedChange("entitlement", "delete", "alice@example.test", "PJM", "*", reason="x"),
        )


def user(user_id, reason="joins the desk", **values):
    return ProposedChange("user", "set", user_id, values=values, reason=reason)


def screen_order(conn, cl, exchange_user, trader=""):
    """An order from the ElectronX screen as the recorder keeps it, with one event."""
    row = {"cl": cl, "user": exchange_user, "trader": trader}
    cur = conn.cursor()
    cur.execute(
        "INSERT electronx.orders (cl_ord_id, version, trader, symbol, side, qty, price, tif, status, order_id,"
        " cum_qty, avg_px, message, last_stream_id, source, exchange_user) VALUES (%(cl)s, 1, %(trader)s, 'S',"
        " 'BUY', 1, 45, 'DAY', 'new', 'O1', 0, 0, '', '1-0', 'electronx', %(user)s)",
        row,
    )
    cur.execute(
        "INSERT electronx.order_events (stream_id, cl_ord_id, version, trader, symbol, side, qty, price, tif,"
        " status, order_id, cum_qty, avg_px, message, source, exchange_user) VALUES (%(cl)s, %(cl)s, 1,"
        " %(trader)s, 'S', 'BUY', 1, 45, 'DAY', 'new', 'O1', 0, 0, '', 'electronx', %(user)s)",
        row,
    )
    conn.commit()


def traders_of(conn, table):
    cur = conn.cursor()
    cur.execute(f"SELECT cl_ord_id, trader FROM electronx.{table} ORDER BY cl_ord_id")
    return cur.fetchall()


def test_a_user_is_their_electronx_login_with_roles_and_accounts(db):
    change_id = propose(
        db, "bob@example.test", user("Jane@Example.com", name="Jane", roles="trader", accounts=["ta-huron-00001"])
    )
    approve(db, change_id, "erin@example.test")
    jane = next(u for u in current_rules(db)["users"] if u["user_id"] == "jane@example.com")
    assert (jane["name"], jane["roles"], jane["accounts"], jane["active"]) == (
        "Jane",
        ["trader"],
        ["TA-HURON-00001"],
        True,
    )
    rules = load_rules(db)
    assert rules.trader_for("JANE@example.com") == "jane@example.com"
    assert rules.account_for("jane@example.com")[0].account_id == "TA-HURON-00001"
    with pytest.raises(ChangeError, match="no ElectronX account TA-NOPE"):
        propose(db, "bob@example.test", user("jane@example.com", accounts=["TA-NOPE"]))
    # Changing roles keeps the rest; taking every role away leaves a user who can do nothing.
    approve(db, propose(db, "bob@example.test", user("jane@example.com", roles=["risk"])), "erin@example.test")
    assert "is not a trader" in load_rules(db).check_trader("jane@example.com", PJM)
    assert load_rules(db).traders["jane@example.com"].accounts == ("TA-HURON-00001",)
    approve(
        db,
        propose(db, "bob@example.test", ProposedChange("user", "delete", "jane@example.com", reason="left")),
        "erin@example.test",
    )
    assert "jane@example.com" not in load_rules(db).traders


def test_adding_a_user_gives_them_their_screen_orders(db):
    screen_order(db, "UI-1", "Zed@x.com")
    screen_order(db, "UI-2", "nobody@x.com")
    screen_order(db, "UI-3", "zed@x.com", trader="erin@example.test")  # already someone's: kept
    assert {(u.exchange_user, u.orders, u.owner) for u in unknown_users(db)} == {
        ("Zed@x.com", 1, None),
        ("nobody@x.com", 1, None),
    }
    approve(db, propose(db, "bob@example.test", user("zed@x.com", roles=["trader"])), "erin@example.test")
    for table in ("orders", "order_events"):
        assert traders_of(db, table) == [("UI-1", "zed@x.com"), ("UI-2", ""), ("UI-3", "erin@example.test")]
    assert [u.exchange_user for u in unknown_users(db)] == ["nobody@x.com"]

    cur = db.cursor()  # added in SSMS instead: only check-traders --fill-in catches up
    cur.execute("INSERT electronx.users (user_id, display_name) VALUES (N'nobody@x.com', N'Nobody')")
    db.commit()
    assert [(u.exchange_user, u.owner) for u in unknown_users(db)] == [("nobody@x.com", "nobody@x.com")]
    assert fill_in_traders(db) == {"orders": 1, "order_events": 1}
    db.commit()
    assert unknown_users(db) == [] and traders_of(db, "orders")[1] == ("UI-2", "nobody@x.com")
