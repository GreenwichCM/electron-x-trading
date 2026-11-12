"""The trading rules endpoints, per role (skipped without SQL Server)."""

import time
from dataclasses import replace

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from trading_api import Settings, create_app
from trading_db import migrate, seed_dev_rules

SETTINGS = Settings(dev_login=True, dev_secret="test-secret-" + "x" * 32)


class NoOrderEntry:
    async def list(self, trader=None):
        return []


def headers(client, user_id):
    for _ in range(100):  # the users load from SQL Server a moment after the api starts
        res = client.post("/api/dev/login", json={"user_id": user_id})
        if res.status_code == 200:
            break
        time.sleep(0.05)
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture
def client(sql_server):
    migrate(sql_server)
    seed_dev_rules(sql_server)
    app = create_app(
        redis=FakeAsyncRedis(),
        settings=replace(SETTINGS, db=sql_server, rules_refresh_s=0.2),
        orderentry=NoOrderEntry(),
    )
    with TestClient(app) as c:
        yield c


def test_who_sees_which_rules(client):
    alice = client.get("/api/rules", headers=headers(client, "alice@example.test")).json()
    assert [u["user_id"] for u in alice["users"]] == ["alice@example.test"]
    assert {r["trader_id"] for r in alice["limits"]} == {"alice@example.test", "*"}  # her own and the desk default
    assert alice["can_edit"] is False and alice["can_approve"] is False
    carol = client.get("/api/rules", headers=headers(client, "carol@example.test")).json()
    assert len(carol["users"]) == 5 and carol["accounts"][0]["account_id"] == "TA-HURON-00001"
    assert carol["can_edit"] is False
    assert client.get("/api/rules/changes", headers=headers(client, "alice@example.test")).status_code == 403
    assert client.get("/api/rules/changes", headers=headers(client, "carol@example.test")).json() == []


def test_propose_and_approve_need_two_risk_users(client):
    change = {"kind": "entitlement", "trader_id": "alice@example.test", "market": "PJM", "reason": "alice covers PJM"}
    for user in ("alice@example.test", "carol@example.test"):
        assert client.post("/api/rules/changes", json=change, headers=headers(client, user)).status_code == 403
    bob, erin = headers(client, "bob@example.test"), headers(client, "erin@example.test")
    assert client.post("/api/rules/changes", json={**change, "reason": ""}, headers=bob).status_code == 400
    change_id = client.post("/api/rules/changes", json=change, headers=bob).json()["change_id"]

    own = client.post(f"/api/rules/changes/{change_id}/approve", headers=bob)
    assert own.status_code == 403 and "second person" in own.json()["detail"]
    assert client.post(f"/api/rules/changes/{change_id}/approve", headers=erin).json()["status"] == "approved"
    rules = client.get("/api/rules", headers=bob).json()
    assert {"trader_id": "alice@example.test", "market": "PJM", "product": "*"} in rules["entitlements"]
    (logged,) = client.get("/api/rules/changes", headers=bob).json()
    assert (logged["status"], logged["requested_by"], logged["decided_by"]) == (
        "approved",
        "bob@example.test",
        "erin@example.test",
    )


def test_without_a_database_the_rules_answer_503():
    app = create_app(redis=FakeAsyncRedis(), settings=SETTINGS, orderentry=NoOrderEntry())
    with TestClient(app) as client:
        assert client.get("/api/rules", headers=headers(client, "bob@example.test")).status_code == 503


def test_the_sign_in_page_lists_the_users_in_the_database(client):
    listed = {u["id"]: u for u in client.get("/api/dev/users").json()}
    for _ in range(100):  # they load with the rules, a moment after the api starts
        if listed:
            break
        listed = {u["id"]: u for u in client.get("/api/dev/users").json()}
    assert set(listed) == {f"{n}@example.test" for n in ("alice", "bob", "carol", "dave", "erin")}
    assert listed["bob@example.test"]["roles"] == ["risk"] and listed["alice@example.test"]["name"] == "Alice (trader)"
    assert client.post("/api/dev/login", json={"user_id": "alice"}).status_code == 404


def test_a_new_user_proposed_and_approved_can_sign_in(client):
    bob, erin = headers(client, "bob@example.test"), headers(client, "erin@example.test")
    change = {
        "kind": "user",
        "trader_id": "Jane@Example.com",
        "values": {"name": "Jane", "roles": ["trader"], "accounts": ["TA-HURON-00001"]},
        "reason": "joins the desk",
    }
    change_id = client.post("/api/rules/changes", json=change, headers=bob).json()["change_id"]
    assert client.post(f"/api/rules/changes/{change_id}/approve", headers=erin).json()["status"] == "approved"
    jane = next(u for u in client.get("/api/rules", headers=bob).json()["users"] if u["user_id"] == "jane@example.com")
    assert (jane["roles"], jane["accounts"]) == (["trader"], ["TA-HURON-00001"])
    me = client.get("/api/me", headers=headers(client, "jane@example.com")).json()  # once the rules reload
    assert me["id"] == "jane@example.com" and "orders.trade" in me["permissions"]
