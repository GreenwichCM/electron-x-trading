"""Which environment the api serves, and record-only (no orderentry)."""

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from trading_api import Settings, create_app
from trading_api.settings import ConfigError
from trading_core import WrongEnvironment

SECRET = "test-secret-" + "x" * 32


class NoCalls:
    """orderentry that must never be called (record-only)."""

    def __getattr__(self, name):
        raise AssertionError(f"orderentry.{name} called in record-only")

    async def aclose(self):
        pass


def login(client, user_id="alice@example.test"):
    token = client.post("/api/dev/login", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_environment_needs_no_sign_in():
    app = create_app(redis=FakeAsyncRedis(), settings=Settings(dev_login=True, dev_secret=SECRET))
    with TestClient(app) as client:
        assert client.get("/api/environment").json() == {"name": "development", "order_entry": True}


def test_record_only_never_calls_orderentry():
    settings = Settings(dev_login=True, dev_secret=SECRET, environment="beta", order_entry=False)
    app = create_app(redis=FakeAsyncRedis(), settings=settings, orderentry=NoCalls())
    with TestClient(app) as client:
        assert client.get("/api/environment").json() == {"name": "beta", "order_entry": False}
        trader = login(client)
        assert client.get("/api/orders", headers=trader).json() == []
        new = {"symbol": "X", "side": "BUY", "qty": 1, "price": 10.0}
        res = client.post("/api/orders", json=new, headers=trader)
        assert res.status_code == 409 and "record-only" in res.json()["detail"]
        assert client.post("/api/orders/abc/cancel", headers=trader).status_code == 409
        risk = login(client, "bob@example.test")
        assert client.get("/api/positions", headers=risk).json()["by_trader"] == {}
        with client.websocket_connect("/ws/orders") as ws:
            ws.send_json({"type": "auth", "token": trader["Authorization"].split()[1]})
            assert ws.receive_json()["type"] == "welcome"


def test_production_is_record_only_for_now():
    with pytest.raises(ConfigError, match="record-only"):
        Settings(dev_login=True, dev_secret=SECRET, environment="production")
    env = {"API_DEV_LOGIN": "Y", "API_DEV_SECRET": SECRET, "TRADING_ENV": "production", "ORDER_ENTRY": "N"}
    settings = Settings.from_env(env)
    assert settings.environment == "production" and not settings.order_entry
    with pytest.raises(ConfigError, match="TRADING_ENV must be one of"):
        Settings.from_env({**env, "TRADING_ENV": "live"})


def test_the_api_refuses_another_environments_redis():
    redis = FakeAsyncRedis(server=FakeServer())
    settings = Settings(dev_login=True, dev_secret=SECRET, environment="production", order_entry=False)
    with TestClient(create_app(redis=redis, settings=settings)):
        pass  # an empty Redis database: production marks it
    beta = Settings(dev_login=True, dev_secret=SECRET, environment="beta")
    with pytest.raises(WrongEnvironment, match="belongs to production"):
        with TestClient(create_app(redis=redis, settings=beta)):
            pass
