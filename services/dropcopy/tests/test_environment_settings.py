"""Production drop copy connects to ElectronX itself, over TLS."""

import pytest
from trading_dropcopy.settings import ConfigError, Settings

PROD = {"TRADING_ENV": "production", "DC_PASSWORD": "p", "DC_HOST": "203.0.113.10", "DC_TLS": "Y"}


def test_production_settings():
    assert Settings.from_env(PROD).environment == "production"


@pytest.mark.parametrize(("change", "message"), [({"DC_TLS": "N"}, "DC_TLS=Y"), ({"DC_HOST": ""}, "the simulator")])
def test_production_refuses(change, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(PROD | change)
