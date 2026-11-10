from datetime import UTC, date, datetime

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


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_expiry_is_the_end_of_the_hour_in_the_iso_time_zone():
    # the examples in the ElectronX docs: ERCOT HE17 on 5 July expires 22:00Z; HE01 on 23 Nov at 07:00Z
    assert Symbol.parse("BFUT500-ERCOT-HB_NORTH-260705-17").expires_at == utc(2026, 7, 5, 22)
    assert Symbol.parse("BFUT100-ERCOT-HB_NORTH-251123-01").expires_at == utc(2025, 11, 23, 7)
    assert Symbol.parse("BFUT500-PJM-WESTERN_HUB-260705-17").expires_at == utc(2026, 7, 5, 21)  # Eastern
    assert Symbol.parse("BFUT100-CAISO-HB_SP15-260705-17").expires_at == utc(2026, 7, 6, 0)  # Pacific
    assert Symbol.parse("BFUT100-MISO-INDIANA-260705-17").expires_at == utc(2026, 7, 5, 22)  # EST in summer


def test_daylight_saving_hours():
    # 1 Nov 2026, clocks fall back at 02:00 CDT: HE02 ends at 01:00 CST, the repeated hour X2 an hour later
    first, again = Symbol.parse("BFUT500-ERCOT-HB_NORTH-261101-02"), Symbol.parse("BFUT500-ERCOT-HB_NORTH-261101-X2")
    assert (first.expiry_hour, first.repeated_hour, again.expiry_hour, again.repeated_hour) == (2, False, 2, True)
    assert first.expires_at == utc(2026, 11, 1, 7) and again.expires_at == utc(2026, 11, 1, 8)
    assert Symbol.parse("BFUT500-ERCOT-HB_NORTH-261101-03").expires_at == utc(2026, 11, 1, 9)
    assert Symbol.parse("BFUT500-ERCOT-HB_NORTH-260308-04").expires_at == utc(2026, 3, 8, 9)  # no HE03 in March


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
