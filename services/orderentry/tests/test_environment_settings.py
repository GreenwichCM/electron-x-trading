"""Production is record-only for now: orderentry does not run there."""

import pytest
from trading_orderentry.settings import ConfigError, Settings


def test_orderentry_refuses_production():
    env = {"TRADING_ENV": "production", "OE_PASSWORD": "p", "OE_INTERNAL_TOKEN": "t" * 32, "DB_PASSWORD": "d"}
    with pytest.raises(ConfigError, match="record-only"):
        Settings.from_env(env)
    assert Settings.from_env(env | {"TRADING_ENV": "beta"}).db.environment == "beta"
