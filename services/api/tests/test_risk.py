"""The /api/risk view per role, and blocking traders."""

import json

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from trading_api import Settings, create_app
from trading_core import TraderBlock

SETTINGS = Settings(dev_login=True, dev_secret="test-secret-" + "x" * 32)
SNAPSHOT = {
    "time": "t",
    "trading_day": {"day": "2026-11-10", "starts_at": "2026-11-10T00:00:00-05:00", "tz": "America/New_York"},
    "traders": {
        "alice": {"positions": {"X": {"qty": 2}}, "realized": 0.0, "unrealized": 5.0, "total": 5.0},
        "dave": {"positions": {"X": {"qty": -1}}, "realized": 0.0, "unrealized": -1.5, "total": -1.5},
    },
    "desk": {"realized": 0.0, "unrealized": 3.5, "total": 3.5, "positions": {"X": 1}},
    "unattributed_fills": 0,
}


class NoOrderEntry:
    async def list(self, trader=None):
        return []


def make_app():
    redis = FakeAsyncRedis()
    return redis, create_app(redis=redis, settings=SETTINGS, orderentry=NoOrderEntry())


def headers(client, user_id):
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_traders_see_only_their_own_risk_plus_desk_totals():
    redis, app = make_app()
    with TestClient(app) as client:
        client.portal.call(redis.set, "risk:snapshot", json.dumps(SNAPSHOT))
        alice = client.get("/api/risk", headers=headers(client, "alice")).json()
        assert set(alice["traders"]) == {"alice"}  # dave's row never reaches alice
        assert alice["desk"]["total"] == 3.5 and alice["trading_day"]["day"] == "2026-11-10"
        for user in ("bob", "carol"):
            everyone = client.get("/api/risk", headers=headers(client, user)).json()
            assert set(everyone["traders"]) == {"alice", "dave"}


def test_blocks_show_up_and_only_risk_can_change_them():
    redis, app = make_app()
    with TestClient(app) as client:
        client.portal.call(redis.set, "risk:snapshot", json.dumps(SNAPSHOT))
        block = TraderBlock(reason="loss limit", by="riskmonitor", time="t")
        client.portal.call(redis.hset, "risk:blocked", "alice", block.to_json())
        alice = client.get("/api/risk", headers=headers(client, "alice")).json()
        assert alice["traders"]["alice"]["blocked"]["reason"] == "loss limit"

        body = {"blocked": False}
        for user in ("alice", "carol"):
            assert client.post("/api/risk/blocks/alice", json=body, headers=headers(client, user)).status_code == 403
        bob = headers(client, "bob")
        assert client.post("/api/risk/blocks/alice", json=body, headers=bob).json() == {
            "trader": "alice",
            "blocked": False,
        }
        assert client.get("/api/risk", headers=bob).json()["traders"]["alice"]["blocked"] is None
        assert client.post("/api/risk/blocks/dave", json={"blocked": True}, headers=bob).status_code == 400
        client.post("/api/risk/blocks/dave", json={"blocked": True, "reason": "review"}, headers=bob)
        assert client.get("/api/risk", headers=bob).json()["traders"]["dave"]["blocked"]["by"] == "bob"
        assert len(client.portal.call(redis.xrange, "alerts")) == 2
