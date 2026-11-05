"""The api against fakeredis. TestClient runs the app in its own event loop; `portal.call` runs a
coroutine inside that loop, which is how the tests write to Redis while the app is running."""

import json

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from trading_api import Settings, create_app

SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


def book(bid, ask, symbol=SYMBOL):
    return {"symbol": symbol, "stale": False, "bid": bid, "ask": ask}


def make_app():
    redis = FakeAsyncRedis()
    return redis, create_app(redis=redis, settings=Settings())


def test_health():
    _, app = make_app()
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_instruments_and_latest_book():
    redis, app = make_app()
    with TestClient(app) as client:
        client.portal.call(redis.set, f"instrument:{SYMBOL}", json.dumps({"symbol": SYMBOL, "tick_size": 0.25}))
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book(40.0, 40.5)))
        assert client.get("/api/instruments").json() == [{"symbol": SYMBOL, "tick_size": 0.25}]
        assert client.get(f"/api/md/{SYMBOL}").json()["bid"] == 40.0
        assert client.get("/api/md/NOPE").status_code == 404


def test_websocket_sends_latest_then_live_updates():
    redis, app = make_app()
    with TestClient(app) as client:
        client.portal.call(redis.set, f"md:last:{SYMBOL}", json.dumps(book(40.0, 40.5)))
        client.portal.call(app.state.hub.ready.wait)
        with client.websocket_connect("/ws/md") as ws:
            first = ws.receive_json()
            assert first == {"type": "book", "data": book(40.0, 40.5)}
            client.portal.call(redis.publish, f"md.{SYMBOL}", json.dumps(book(40.25, 40.5)))
            assert ws.receive_json()["data"]["bid"] == 40.25


def test_each_browser_gets_its_own_feed():
    redis, app = make_app()
    with TestClient(app) as client:
        client.portal.call(app.state.hub.ready.wait)
        with client.websocket_connect("/ws/md") as a, client.websocket_connect("/ws/md") as b:
            client.portal.call(redis.publish, f"md.{SYMBOL}", json.dumps(book(41.0, 41.5)))
            assert a.receive_json()["data"]["bid"] == 41.0
            assert b.receive_json()["data"]["bid"] == 41.0
        assert app.state.hub.clients == set()  # both cleaned up after closing
