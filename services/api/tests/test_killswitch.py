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


def test_settled_contracts_leave_the_positions_check_unless_they_break():
    redis, app = make_app()
    with TestClient(app) as client:
        state = {"positions": {SYMBOL: 2}, "connected": True, "time": "t"}
        client.portal.call(redis.set, "dc:positions", json.dumps(state))
        book = {"symbol": SYMBOL, "settlement": {"price": 44.0, "final": True}}
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book))
        body = client.get("/api/positions", headers=headers(client, "bob")).json()
        assert body["exchange"] == {} and body["by_trader"] == {"alice": {}} and body["settled"] == {SYMBOL: 44.0}

        state["positions"][SYMBOL] = 3  # the two sides disagree: keep showing it
        client.portal.call(redis.set, "dc:positions", json.dumps(state))
        body = client.get("/api/positions", headers=headers(client, "bob")).json()
        assert body["exchange"] == {SYMBOL: 3} and body["settled"] == {}


def test_trading_on_the_electronx_screen_is_shown_apart_and_is_not_a_break():
    redis, app = make_app()
    with TestClient(app) as client:
        state = {
            "positions": {SYMBOL: 5},
            "screen": {SYMBOL: 3},
            "screen_by_user": {"firms/h/users/dave": {SYMBOL: 3}},
            "connected": True,
            "time": "t",
        }
        client.portal.call(redis.set, "dc:positions", json.dumps(state))
        body = client.get("/api/positions", headers=headers(client, "bob")).json()
        assert body["exchange"] == {SYMBOL: 5} and body["screen"] == {SYMBOL: 3}
        assert body["by_trader"] == {"alice": {SYMBOL: 2}} and body["screen_by_user"] == state["screen_by_user"]

        book = {"symbol": SYMBOL, "settlement": {"price": 44.0, "final": True}}
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book))
        body = client.get("/api/positions", headers=headers(client, "bob")).json()
        assert body["settled"] == {SYMBOL: 44.0} and body["exchange"] == body["screen"] == body["screen_by_user"] == {}


def test_a_fill_not_placed_yet_is_neither_a_match_nor_a_break():
    redis, app = make_app()
    with TestClient(app) as client:
        state = {"positions": {SYMBOL: 3}, "pending": {SYMBOL: 1}, "connected": True, "time": "t"}
        client.portal.call(redis.set, "dc:positions", json.dumps(state))
        book = {"symbol": SYMBOL, "settlement": {"price": 44.0, "final": True}}
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book))
        body = client.get("/api/positions", headers=headers(client, "bob")).json()
        assert body["pending"] == {SYMBOL: 1} and body["settled"] == {}  # not left out while being checked
