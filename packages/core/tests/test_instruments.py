from datetime import UTC, date, datetime, timedelta

import pytest
from trading_core import DATA_START_DATE, Market, Product, Symbol


def test_symbol_parse():
    s = Symbol.parse("BFUT500-ERCOT-HB_NORTH-260701-18")
    assert s.market is Market.ERCOT and s.product is Product.BFUT
    assert s.product_code == "BFUT500" and s.location == "HB_NORTH"
    assert s.expiry == datetime(2026, 7, 1, 18)
    assert str(s) == "BFUT500-ERCOT-HB_NORTH-260701-18"


def test_symbol_parse_hyphenated_market_and_hour_24():
    s = Symbol.parse("BFUT-ISO-NE-.H.INTERNAL_HUB-260901-24")
    assert s.market is Market.ISO_NE and s.product_variant is None
    assert s.expiry == datetime(2026, 9, 2, 0)


def test_expires_at_is_new_york_time():
    s = Symbol.parse("BFUT500-ERCOT-HB_NORTH-261110-18")
    assert s.expires_at == datetime(2026, 11, 10, 23, tzinfo=UTC)  # 18:00 EST
    assert Symbol.parse("BFUT500-ERCOT-HB_NORTH-260701-18").expires_at.utcoffset() == timedelta(hours=-4)  # EDT


@pytest.mark.parametrize("bad", ["AAPL", "BFUT500-ERCOT-HB_NORTH-260701", "XFUT-ERCOT-HB_NORTH-260701-18"])
def test_symbol_parse_rejects_garbage(bad):
    with pytest.raises(ValueError):
        Symbol.parse(bad)


def test_symbol_is_immutable():
    s = Symbol.parse("BOPT-PJM-WESTERN_HUB-261015-09")
    with pytest.raises(ValueError):
        s.location = "OTHER"


def test_data_start_is_ercot_launch():
    assert DATA_START_DATE == date(2025, 12, 10)
