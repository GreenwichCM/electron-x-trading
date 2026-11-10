"""The api against fakeredis. TestClient runs the app in its own event loop; `portal.call` runs a
coroutine inside that loop, which is how the tests write to Redis while the app is running."""

import json

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from trading_api import Settings, User, create_app

SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"
SETTINGS = Settings(dev_login=True, dev_secret="test-secret-" + "x" * 32)


def book(bid, ask, symbol=SYMBOL):
    return {"symbol": symbol, "stale": False, "bid": bid, "ask": ask}


def make_app(**kwargs):
    redis = FakeAsyncRedis()
    return redis, create_app(redis=redis, settings=SETTINGS, **kwargs)


def login(client, user_id="alice"):
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return token, {"Authorization": f"Bearer {token}"}


def test_health_needs_no_login():
    _, app = make_app()
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_everything_else_needs_a_valid_token():
    _, app = make_app()
    with TestClient(app) as client:
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/instruments").status_code == 401
        assert client.get("/api/me", headers={"Authorization": "Bearer forged"}).status_code == 401


def test_me_shows_role_permissions():
    _, app = make_app()
    with TestClient(app) as client:
        _, headers = login(client, "bob")
        me = client.get("/api/me", headers=headers).json()
        assert me["id"] == "bob" and me["roles"] == ["risk"]
        assert "killswitch.trip" in me["permissions"] and "orders.trade" not in me["permissions"]


def test_unknown_test_user_cannot_log_in():
    _, app = make_app()
    with TestClient(app) as client:
        assert client.post("/api/dev/login", json={"user_id": "mallory"}).status_code == 404


def test_missing_permission_is_forbidden():
    class NoMarketData:
        def authenticate(self, token):
            return User("dave", "Dave", ("nobody",))

    _, app = make_app(authenticator=NoMarketData())
    with TestClient(app) as client:
        assert client.get("/api/instruments", headers={"Authorization": "Bearer t"}).status_code == 403


def test_instruments_and_latest_book():
    redis, app = make_app()
    with TestClient(app) as client:
        _, headers = login(client)
        client.portal.call(redis.set, f"instrument:{SYMBOL}", json.dumps({"symbol": SYMBOL, "tick_size": 0.25}))
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book(40.0, 40.5)))
        client.portal.call(app.state.hub.load_instruments)  # every 30 s in the background
        assert client.get("/api/instruments", headers=headers).json() == [
            {"symbol": SYMBOL, "state": None, "last_trade_time": None}  # only what the price screen needs
        ]
        assert client.get(f"/api/md/{SYMBOL}", headers=headers).json()["bid"] == 40.0
        assert client.get("/api/md/NOPE", headers=headers).status_code == 404


def test_websocket_rejects_a_bad_token():
    _, app = make_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/md") as ws:
            ws.send_json({"type": "auth", "token": "forged"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401


def test_websocket_sends_latest_then_live_updates():
    redis, app = make_app()
    with TestClient(app) as client:
        token, _ = login(client)
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book(40.0, 40.5)))
        client.portal.call(app.state.hub.ready.wait)
        with client.websocket_connect("/ws/md") as ws:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["user"]["id"] == "alice"
            assert ws.receive_json() == {"type": "book", "data": book(40.0, 40.5)}
            client.portal.call(redis.publish, f"md.{SYMBOL}", json.dumps(book(40.25, 40.5)))
            assert ws.receive_json()["data"]["bid"] == 40.25


def test_each_browser_gets_its_own_feed():
    redis, app = make_app()
    with TestClient(app) as client:
        token, _ = login(client)
        client.portal.call(app.state.hub.ready.wait)
        with client.websocket_connect("/ws/md") as a, client.websocket_connect("/ws/md") as b:
            for ws in (a, b):
                ws.send_json({"type": "auth", "token": token})
                assert ws.receive_json()["type"] == "welcome"
            client.portal.call(redis.publish, f"md.{SYMBOL}", json.dumps(book(41.0, 41.5)))
            assert a.receive_json()["data"]["bid"] == 41.0
            assert b.receive_json()["data"]["bid"] == 41.0
        assert app.state.hub.clients == set()  # both cleaned up after closing


def test_a_browser_that_watches_gets_only_the_books_it_shows():
    redis, app = make_app()
    other = "BFUT500-ERCOT-HB_NORTH-261231-16"
    with TestClient(app) as client:
        token, _ = login(client)
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book(40.0, 40.5)))
        client.portal.call(redis.set, f"md:last:{other}", json.dumps(book(41.0, 41.5, other)))
        client.portal.call(app.state.hub.ready.wait)
        with client.websocket_connect("/ws/md?watch=1") as ws:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["type"] == "welcome"
            ws.send_json({"type": "watch", "symbols": [other]})
            assert ws.receive_json() == {"type": "book", "data": book(41.0, 41.5, other)}  # its latest book
            client.portal.call(redis.publish, f"md.{SYMBOL}", json.dumps(book(40.25, 40.5)))  # not shown: skipped
            client.portal.call(redis.publish, f"md.{other}", json.dumps(book(41.25, 41.5, other)))
            assert ws.receive_json()["data"] == book(41.25, 41.5, other)

            ws.send_json({"type": "watch", "symbols": [SYMBOL]})  # the screen changed
            assert ws.receive_json()["data"] == book(40.0, 40.5)


def test_the_instrument_list_is_compressed_and_not_sent_again_while_unchanged():
    redis, app = make_app()
    with TestClient(app) as client:
        _, headers = login(client)
        for n in range(300):
            symbol = f"BFUT500-ERCOT-HB_T{n}-261231-15"
            client.portal.call(redis.set, f"instrument:{symbol}", json.dumps({"symbol": symbol, "state": "OPEN"}))
        client.portal.call(app.state.hub.load_instruments)
        first = client.get("/api/instruments", headers={**headers, "Accept-Encoding": "gzip"})
        assert first.status_code == 200 and first.headers["content-encoding"] == "gzip" and len(first.json()) == 300
        etag = first.headers["etag"]
        again = client.get("/api/instruments", headers={**headers, "If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""

        client.portal.call(redis.set, "instrument:BFUT500-PJM-WESTERN_HUB-261231-15", json.dumps({"symbol": "x"}))
        client.portal.call(app.state.hub.load_instruments)
        changed = client.get("/api/instruments", headers={**headers, "If-None-Match": etag})
        assert changed.status_code == 200 and changed.headers["etag"] != etag


def test_marketdata_is_told_what_the_browsers_show():
    redis, app = make_app()
    other = "BFUT500-ERCOT-HB_NORTH-261231-16"
    with TestClient(app) as client:
        token, _ = login(client)
        client.portal.call(app.state.hub.ready.wait)
        with client.websocket_connect("/ws/md?watch=1") as a, client.websocket_connect("/ws/md?watch=1") as b:
            for ws in (a, b):
                ws.send_json({"type": "auth", "token": token})
                assert ws.receive_json()["type"] == "welcome"
            a.send_json({"type": "watch", "symbols": [SYMBOL]})
            b.send_json({"type": "watch", "symbols": [SYMBOL, other]})
            key = next(iter(client.portal.call(redis.keys, "md:wanted:*")), None)
            for _ in range(100):
                key = next(iter(client.portal.call(redis.keys, "md:wanted:*")), None)
                if key and len(json.loads(client.portal.call(redis.get, key))) == 2:
                    break
                client.portal.call(__import__("asyncio").sleep, 0.05)
            assert json.loads(client.portal.call(redis.get, key)) == sorted([SYMBOL, other])
            assert 0 < client.portal.call(redis.ttl, key) <= 90  # gone soon if this api stops


def test_an_empty_instrument_list_still_has_an_etag():
    _, app = make_app()
    with TestClient(app) as client:
        _, headers = login(client)
        first = client.get("/api/instruments", headers=headers)
        assert first.json() == [] and first.headers["etag"]
        assert (
            client.get("/api/instruments", headers={**headers, "If-None-Match": first.headers["etag"]}).status_code
            == 304
        )
