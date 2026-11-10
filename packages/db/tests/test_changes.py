"""Proposing and approving rule changes (four eyes). Validation runs without SQL Server; the rest is
skipped without one."""

from contextlib import closing

import pytest
from trading_db import connect, load_rules, migrate, seed_dev_rules
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


def limit(trader="alice", market="*", product="*", reason="r", action="set", **values):
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
        (ProposedChange("trader", "set", "*", reason="r"), "real user id"),
        (ProposedChange("entitlement", "set", "alice", "PJM", "*", {"x": 1}, "r"), "no values"),
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
    change_id = propose(db, "bob", ProposedChange("entitlement", "set", "alice", "PJM", "*", reason="alice covers PJM"))
    assert "not entitled" in load_rules(db).check_trader("alice", PJM)  # nothing changes until approved
    with pytest.raises(ChangeError, match="second person") as exc:
        approve(db, change_id, "bob")
    assert exc.value.status == 403
    approve(db, change_id, "erin")
    assert load_rules(db).check_trader("alice", PJM) is None
    (change,) = list_changes(db)
    assert (change["status"], change["requested_by"], change["decided_by"]) == ("approved", "bob", "erin")
    assert (change["before"], change["after"]) == (None, {})
    with pytest.raises(ChangeError, match="already approved"):
        approve(db, change_id, "erin")


def test_limits_keep_before_and_after(db):
    change_id = propose(db, "bob", limit("dave", "PJM", "BFUT100", max_position=5, reason="new trader"))
    approve(db, change_id, "erin")
    assert load_rules(db).limits_for("dave", PJM).max_position == 5
    change_id = propose(db, "erin", limit("dave", "PJM", "BFUT100", max_position=8, max_order_qty=2, reason="more"))
    approve(db, change_id, "bob")
    rules = load_rules(db).limits_for("dave", PJM)
    assert (rules.max_position, rules.max_order_qty) == (8, 2)
    latest = list_changes(db)[0]
    assert latest["before"]["max_position"] == 5 and latest["after"]["max_position"] == 8
    with pytest.raises(ChangeError, match="already the rule"):
        propose(db, "bob", limit("dave", "PJM", "BFUT100", max_position=8, max_order_qty=2))


def test_a_proposal_overtaken_by_another_change_goes_stale(db):
    first = propose(db, "bob", ProposedChange("trader", "set", "dave", values={"active": False}, reason="leave"))
    second = propose(db, "erin", ProposedChange("trader", "set", "dave", values={"note": "PJM desk"}, reason="note"))
    approve(db, first, "erin")
    with pytest.raises(ChangeError, match="changed after") as exc:
        approve(db, second, "bob")  # would quietly make dave active again
    assert exc.value.status == 409
    assert statuses(db) == {first: "approved", second: "stale"}
    assert load_rules(db).check_trader("dave", PJM) == "dave is not active"


def test_reject_withdraw_and_remove(db):
    a = propose(
        db, "bob", ProposedChange("trader", "set", "frank", values={"exchange_trader_id": "frank@x"}, reason="new")
    )
    b = propose(db, "bob", ProposedChange("entitlement", "delete", "alice", "ERCOT", "*", reason="moves desk"))
    c = propose(
        db,
        "bob",
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
        reject(db, a, "bob")
    reject(db, a, "erin", "not hired yet")
    with pytest.raises(ChangeError, match="only whoever proposed"):
        withdraw(db, b, "erin")
    withdraw(db, b, "bob")
    approve(db, c, "erin")
    assert statuses(db) == {a: "rejected", b: "withdrawn", c: "approved"}
    rules = current_rules(db)
    assert [t["trader_id"] for t in rules["traders"]] == ["alice", "dave"]
    assert {"trader_id": "alice", "market": "ERCOT", "product": "*"} in rules["entitlements"]
    desk = next(r for r in rules["limits"] if r["trader_id"] == "*")
    assert desk["max_order_qty"] == 5 and desk["max_loss"] == 5000.0
    with pytest.raises(ChangeError, match="no such rule"):
        propose(db, "bob", ProposedChange("entitlement", "delete", "alice", "PJM", "*", reason="x"))
