"""The api serves the built web app when API_WEB_DIST is set (production's always-on machine)."""

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from trading_api import Settings, create_app
from trading_api.settings import ConfigError

SECRET = "test-secret-" + "x" * 32


def test_the_web_app_is_served_next_to_the_api(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>ElectronX trading</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    settings = Settings(dev_login=True, dev_secret=SECRET, web_dist=str(tmp_path))
    with TestClient(create_app(redis=FakeAsyncRedis(server=FakeServer()), settings=settings)) as client:
        page = client.get("/")
        assert page.status_code == 200 and "ElectronX trading" in page.text
        assert page.headers["content-type"].startswith("text/html")
        assert client.get("/assets/app.js").text == "console.log(1)"
        assert client.get("/api/health").json() == {"status": "ok"}  # the api still answers first
        assert client.get("/api/environment").json()["name"] == "development"


def test_without_it_nothing_is_served_at_the_root():
    settings = Settings(dev_login=True, dev_secret=SECRET)
    with TestClient(create_app(redis=FakeAsyncRedis(server=FakeServer()), settings=settings)) as client:
        assert client.get("/").status_code == 404


def test_a_missing_build_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="npm --prefix web run build"):
        Settings(dev_login=True, dev_secret=SECRET, web_dist=str(tmp_path / "dist"))
