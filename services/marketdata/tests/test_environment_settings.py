"""Production connects to ElectronX itself, over TLS; each environment uses its own Snapshot API."""

import pytest
from trading_marketdata.settings import ConfigError, Settings

PROD = {
    "TRADING_ENV": "production",
    "MD_PASSWORD": "p",
    "MD_HOST": "203.0.113.10",
    "MD_TLS": "Y",
}


def test_production_settings():
    settings = Settings.from_env(PROD | {"SNAPSHOT_URL": "https://trade.electronx.com", "SNAPSHOT_API_KEY": "k"})
    assert settings.environment == "production" and settings.tls is not None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"MD_TLS": "N"}, "MD_TLS=Y"),
        ({"MD_HOST": "127.0.0.1"}, "the simulator"),
        ({"SNAPSHOT_URL": "https://beta.electronx.com", "SNAPSHOT_API_KEY": "k"}, "the beta one"),
    ],
)
def test_production_refuses(change, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(PROD | change)


def test_beta_refuses_the_production_snapshot_api():
    env = {"TRADING_ENV": "beta", "MD_PASSWORD": "p", "SNAPSHOT_URL": "https://trade.electronx.com"}
    with pytest.raises(ConfigError, match="the production one"):
        Settings.from_env(env | {"SNAPSHOT_API_KEY": "k"})


def test_development_is_the_default():
    assert Settings.from_env({"MD_PASSWORD": "sim"}).environment == "development"
