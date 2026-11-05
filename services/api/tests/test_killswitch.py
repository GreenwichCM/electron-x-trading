"""Kill switch and positions endpoints."""

import json

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from trading_api import OrderEntryUnavailable, Settings, create_app

SETTINGS = Settings(dev_login=True, dev_secret="test-secret-" + "x" * 32)
SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


class FakeOrderEntry:
    def __init__(self, down=False):
        self.down = down

    async def positions(self):
        if self.down:
            raise OrderEntryUnavailable("down")
        return {"alice": {SYMBOL: 2}}

    async def list(self, trader=None):
        return []


def make_app(oe=None):
    redis = FakeAsyncRedis()
    return redis, create_app(redis=redis, settings=SETTINGS, orderentry=oe or FakeOrderEntry())


def headers(client, user_id):
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_everyone_can_see_the_kill_switch_but_only_risk_can_change_it():
    redis, app = make_app()
    with TestClient(app) as client:
        assert client.get("/api/killswitch", headers=headers(client, "alice")).json()["active"] is False
        for user in ("alice", "carol"):
            resp = client.post("/api/killswitch", json={"active": True, "reason": "x"}, headers=headers(client, user))
            assert resp.status_code == 403
        bob = headers(client, "bob")
        assert client.post("/api/killswitch", json={"active": True}, headers=bob).status_code == 400  # no reason
        on = client.post("/api/killswitch", json={"active": True, "reason": "fat finger"}, headers=bob).json()
        assert on["active"] and on["by"] == "bob" and on["reason"] == "fat finger"
        seen = client.get("/api/killswitch", headers=headers(client, "alice")).json()
        assert seen["active"] and seen["reason"] == "fat finger"
        assert client.post("/api/killswitch", json={"active": False}, headers=bob).json()["active"] is False
        assert len(client.portal.call(redis.xrange, "alerts")) == 2  # both changes are on record


def test_positions_are_for_risk_and_management_only():
    redis, app = make_app()
    with TestClient(app) as client:
        state = {"positions": {SYMBOL: 2}, "connected": True, "time": "t"}
        client.portal.call(redis.set, "dc:positions", json.dumps(state))
        assert client.get("/api/positions", headers=headers(client, "alice")).status_code == 403
        for user in ("bob", "carol"):
            body = client.get("/api/positions", headers=headers(client, user)).json()
            assert body["exchange"] == {SYMBOL: 2} and body["dropcopy_connected"] is True
            assert body["by_trader"] == {"alice": {SYMBOL: 2}}


def test_positions_still_show_the_exchange_side_when_orderentry_is_down():
    _, app = make_app(FakeOrderEntry(down=True))
    with TestClient(app) as client:
        body = client.get("/api/positions", headers=headers(client, "bob")).json()
        assert body["exchange"] == {} and body["by_trader"] is None
