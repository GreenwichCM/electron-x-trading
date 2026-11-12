"""Market hours and time & sales."""

import json
from datetime import date

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from trading_api import Settings, create_app
from trading_api.settings import ConfigError

SECRET = "test-secret-" + "x" * 32
A, B = "BFUT500-ERCOT-HB_NORTH-261231-15", "BFUT100-PJM-WESTERN_HUB-261231-15"


def signed_in(client, user_id="alice@example.test"):
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_market_hours():
    settings = Settings(dev_login=True, dev_secret=SECRET, exchange_holidays=frozenset({date(2028, 1, 3)}))
    with TestClient(create_app(redis=FakeAsyncRedis(server=FakeServer()), settings=settings)) as client:
        assert client.get("/api/market-hours").status_code == 401
        hours = client.get("/api/market-hours", headers=signed_in(client)).json()
        assert hours["schedule"] == "Monday to Friday, 7:00am to 8:00pm CT"
        assert isinstance(hours["open"], bool) and hours["next_change"].endswith("+00:00") and hours["reason"]
        upcoming = hours["upcoming"]  # the next holidays and early closes, from the published calendar
        assert 0 < len(upcoming) <= 4 and all(set(u) == {"date", "what"} for u in upcoming)
        assert upcoming == sorted(upcoming, key=lambda u: u["date"])


def test_exchange_holidays_setting():
    env = {"API_DEV_LOGIN": "Y", "API_DEV_SECRET": SECRET, "EXCHANGE_HOLIDAYS": "2028-01-03, 2028-01-17"}
    assert Settings.from_env(env).exchange_holidays == {date(2028, 1, 3), date(2028, 1, 17)}
    with pytest.raises(ConfigError, match="not a date"):
        Settings.from_env({**env, "EXCHANGE_HOLIDAYS": "26/11/2026"})


def test_time_and_sales():
    redis = FakeAsyncRedis(server=FakeServer())
    app = create_app(redis=redis, settings=Settings(dev_login=True, dev_secret=SECRET))
    with TestClient(app) as client:
        headers = signed_in(client)
        assert client.get("/api/trades", headers=headers).json() == {"trades": [], "last_id": ""}

        def add(symbol, price):
            data = {"symbol": symbol, "price": price, "qty": 1.0, "aggressor": "BUY", "trade_id": "T", "time": "t"}
            return client.portal.call(redis.xadd, "md:trades", {"data": json.dumps(data)}).decode()

        add(A, 1.0)
        second = add(B, 2.0)
        third = add(A, 3.0)
        res = client.get("/api/trades", headers=headers).json()
        assert [t["price"] for t in res["trades"]] == [3.0, 2.0, 1.0] and res["last_id"] == third
        assert res["trades"][0]["id"] == third and res["trades"][0]["symbol"] == A
        assert [t["price"] for t in client.get(f"/api/trades?symbol={A}", headers=headers).json()["trades"]] == [
            3.0,
            1.0,
        ]
        newer = client.get(f"/api/trades?after={second}", headers=headers).json()
        assert [t["price"] for t in newer["trades"]] == [3.0] and newer["last_id"] == third
        assert client.get(f"/api/trades?after={third}", headers=headers).json() == {"trades": [], "last_id": third}
        assert client.get("/api/trades?limit=1", headers=headers).json()["trades"][0]["price"] == 3.0
        assert client.get("/api/trades?after=x", headers=headers).status_code == 400
