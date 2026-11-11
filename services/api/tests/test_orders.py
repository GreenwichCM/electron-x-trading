"""Order endpoints and the /ws/orders WebSocket, with a fake orderentry and fakeredis."""

import json

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from trading_api import OrderEntryError, OrderEntryUnavailable, Settings, create_app
from trading_core.rules import Trader, TradingRules

SETTINGS = Settings(dev_login=True, dev_secret="test-secret-" + "x" * 32)
SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


def order(cl, trader, status="new"):
    return {"cl_ord_id": cl, "trader": trader, "symbol": SYMBOL, "side": "BUY", "qty": 1, "status": status}


class FakeOrderEntry:
    def __init__(self, orders=(), down=False):
        self.orders = list(orders)
        self.down = down
        self.submitted = []

    async def submit(self, trader, symbol, side, qty, price, tif):
        if self.down:
            raise OrderEntryUnavailable("connection refused")
        o = {**order(f"C{len(self.orders) + 1}", trader), "symbol": symbol, "price": price, "tif": tif}
        self.orders.append(o)
        self.submitted.append(o)
        return o

    async def cancel(self, trader, cl_ord_id):
        raise OrderEntryError(409, "order is filled")

    async def list(self, trader=None):
        return [o for o in self.orders if trader is None or o["trader"] == trader]


def make_app(oe):
    redis = FakeAsyncRedis()
    return redis, create_app(redis=redis, settings=SETTINGS, orderentry=oe)


def headers(client, user_id):
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return token, {"Authorization": f"Bearer {token}"}


NEW = {"symbol": SYMBOL, "side": "BUY", "qty": 2, "price": 45.0, "tif": "DAY"}


def test_only_traders_can_place_orders():
    oe = FakeOrderEntry()
    _, app = make_app(oe)
    with TestClient(app) as client:
        for user in ("bob", "carol"):
            _, h = headers(client, user)
            assert client.post("/api/orders", json=NEW, headers=h).status_code == 403
        _, h = headers(client, "alice")
        resp = client.post("/api/orders", json=NEW, headers=h)
        assert resp.status_code == 200 and resp.json()["trader"] == "alice"
        assert len(oe.submitted) == 1


def test_trader_is_always_the_signed_in_user():
    oe = FakeOrderEntry()
    _, app = make_app(oe)
    with TestClient(app) as client:
        _, h = headers(client, "alice")
        client.post("/api/orders", json={**NEW, "trader": "bob"}, headers=h)  # extra field is ignored
        assert oe.submitted[-1]["trader"] == "alice"


def test_order_lists_follow_permissions():
    oe = FakeOrderEntry([order("A1", "alice"), order("D1", "dave")])
    _, app = make_app(oe)
    with TestClient(app) as client:
        _, h = headers(client, "alice")
        assert [o["cl_ord_id"] for o in client.get("/api/orders", headers=h).json()] == ["A1"]
        for user in ("bob", "carol"):
            _, h = headers(client, user)
            assert len(client.get("/api/orders", headers=h).json()) == 2


def test_orderentry_errors_are_passed_on():
    _, app = make_app(FakeOrderEntry(down=True))
    with TestClient(app) as client:
        _, h = headers(client, "alice")
        assert client.post("/api/orders", json=NEW, headers=h).status_code == 503
        cancel = client.post("/api/orders/A1/cancel", headers=h)
        assert cancel.status_code == 409 and cancel.json()["detail"] == "order is filled"


def test_order_websocket_shows_each_user_what_they_may_see():
    redis, app = make_app(FakeOrderEntry([order("A0", "alice", "filled")]))
    with TestClient(app) as client:
        alice_token, _ = headers(client, "alice")
        bob_token, _ = headers(client, "bob")
        client.portal.call(app.state.orders.ready.wait)
        with client.websocket_connect("/ws/orders") as alice, client.websocket_connect("/ws/orders") as bob:
            for ws, token in ((alice, alice_token), (bob, bob_token)):
                ws.send_json({"type": "auth", "token": token})
                assert ws.receive_json()["type"] == "welcome"
                assert ws.receive_json()["data"]["cl_ord_id"] == "A0"  # existing order, from orderentry

            for o in (order("D1", "dave"), order("A1", "alice")):
                client.portal.call(redis.xadd, "orders", {"data": json.dumps(o)})
            assert alice.receive_json()["data"]["cl_ord_id"] == "A1"  # dave's order never reaches alice
            seen = {bob.receive_json()["data"]["cl_ord_id"] for _ in range(2)}
            assert seen == {"D1", "A1"}


def screen_order(cl, user, version=1, status="new"):
    return {
        "cl_ord_id": cl,
        "order_id": "O" + cl,
        "trader": "",
        "exchange_user": user,
        "source": "electronx",
        "symbol": SYMBOL,
        "side": "SELL",
        "qty": 2,
        "price": 47.0,
        "tif": "DAY",
        "status": status,
        "cum_qty": 0,
        "avg_px": 0.0,
        "text": "",
        "created": "20261110-14:03:56.123456789",
        "updated": "20261110-14:03:56.123456789",
        "version": version,
    }


def test_orders_from_the_electronx_screen_are_in_the_blotter_for_their_trader():
    redis, app = make_app(FakeOrderEntry([order("A0", "alice", "filled")]))
    with TestClient(app) as client:
        alice_token, alice = headers(client, "alice")
        _, bob = headers(client, "bob")
        client.portal.call(app.state.orders.ready.wait)
        app.state.orders.set_rules(
            TradingRules(traders={"alice": Trader("alice", "a@x.com")}, entitlements=frozenset(), limits={})
        )
        with client.websocket_connect("/ws/orders") as ws:
            ws.send_json({"type": "auth", "token": alice_token})
            assert ws.receive_json()["type"] == "welcome"
            assert ws.receive_json()["data"]["cl_ord_id"] == "A0"
            client.portal.call(redis.xadd, "screen_orders", {"data": json.dumps(screen_order("UI-1", "z@x.com"))})
            client.portal.call(redis.xadd, "screen_orders", {"data": json.dumps(screen_order("UI-2", "a@x.com"))})
            row = ws.receive_json()["data"]  # only alice's own: the other ElectronX id is nobody's here
            assert (row["cl_ord_id"], row["trader"], row["source"]) == ("UI-2", "alice", "electronx")
            assert row["created"] == "2026-11-10T14:03:56.123+00:00" and row["leaves"] == 2

        client.portal.call(
            redis.xadd, "screen_orders", {"data": json.dumps(screen_order("UI-2", "a@x.com", 2, "canceled"))}
        )
        client.portal.call(
            redis.xadd, "screen_orders", {"data": json.dumps(screen_order("UI-2", "a@x.com", 1))}
        )  # late
        for _ in range(100):
            mine = client.get("/api/orders", headers=alice).json()
            if any(o["cl_ord_id"] == "UI-2" and o["status"] == "canceled" for o in mine):
                break
        assert [(o["cl_ord_id"], o["status"]) for o in mine] == [("UI-2", "canceled"), ("A0", "filled")]
        everyone = {o["cl_ord_id"]: o for o in client.get("/api/orders", headers=bob).json()}
        assert set(everyone) == {"A0", "UI-1", "UI-2"} and everyone["UI-1"]["trader"] == ""


def test_risk_is_told_about_electronx_users_no_trader_has():
    redis, app = make_app(FakeOrderEntry())
    with TestClient(app) as client:
        _, alice = headers(client, "alice")
        _, bob = headers(client, "bob")
        client.portal.call(app.state.orders.ready.wait)
        assert client.get("/api/exchange-users/unknown", headers=bob).json() == []  # rules not loaded yet
        app.state.orders.set_rules(
            TradingRules(traders={"alice": Trader("alice", "a@x.com")}, entitlements=frozenset(), limits={})
        )
        for cl, user in (("UI-1", "z@x.com"), ("UI-2", "A@x.com"), ("UI-3", "z@x.com")):
            client.portal.call(redis.xadd, "screen_orders", {"data": json.dumps(screen_order(cl, user))})
        for _ in range(100):
            unknown = client.get("/api/exchange-users/unknown", headers=bob).json()
            if unknown and unknown[0]["orders"] == 2:
                break
        assert [(u["exchange_user"], u["orders"]) for u in unknown] == [("z@x.com", 2)]  # A@x.com is alice
        assert unknown[0]["last"] == "2026-11-10T14:03:56.123+00:00"
        assert client.get("/api/exchange-users/unknown", headers=alice).status_code == 403
