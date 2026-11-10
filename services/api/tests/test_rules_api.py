"""The trading rules endpoints, per role (skipped without SQL Server)."""

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
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(sql_server):
    migrate(sql_server)
    seed_dev_rules(sql_server)
    app = create_app(redis=FakeAsyncRedis(), settings=replace(SETTINGS, db=sql_server), orderentry=NoOrderEntry())
    with TestClient(app) as c:
        yield c


def test_who_sees_which_rules(client):
    alice = client.get("/api/rules", headers=headers(client, "alice")).json()
    assert [t["trader_id"] for t in alice["traders"]] == ["alice"]
    assert {r["trader_id"] for r in alice["limits"]} == {"alice", "*"}  # her own and the desk default
    assert alice["can_edit"] is False and alice["can_approve"] is False
    carol = client.get("/api/rules", headers=headers(client, "carol")).json()
    assert [t["trader_id"] for t in carol["traders"]] == ["alice", "dave"] and carol["can_edit"] is False
    assert client.get("/api/rules/changes", headers=headers(client, "alice")).status_code == 403
    assert client.get("/api/rules/changes", headers=headers(client, "carol")).json() == []


def test_propose_and_approve_need_two_risk_users(client):
    change = {"kind": "entitlement", "trader_id": "alice", "market": "PJM", "reason": "alice covers PJM"}
    for user in ("alice", "carol"):
        assert client.post("/api/rules/changes", json=change, headers=headers(client, user)).status_code == 403
    bob, erin = headers(client, "bob"), headers(client, "erin")
    assert client.post("/api/rules/changes", json={**change, "reason": ""}, headers=bob).status_code == 400
    change_id = client.post("/api/rules/changes", json=change, headers=bob).json()["change_id"]

    own = client.post(f"/api/rules/changes/{change_id}/approve", headers=bob)
    assert own.status_code == 403 and "second person" in own.json()["detail"]
    assert client.post(f"/api/rules/changes/{change_id}/approve", headers=erin).json()["status"] == "approved"
    rules = client.get("/api/rules", headers=bob).json()
    assert {"trader_id": "alice", "market": "PJM", "product": "*"} in rules["entitlements"]
    (logged,) = client.get("/api/rules/changes", headers=bob).json()
    assert (logged["status"], logged["requested_by"], logged["decided_by"]) == ("approved", "bob", "erin")


def test_without_a_database_the_rules_answer_503():
    app = create_app(redis=FakeAsyncRedis(), settings=SETTINGS, orderentry=NoOrderEntry())
    with TestClient(app) as client:
        assert client.get("/api/rules", headers=headers(client, "bob")).status_code == 503
