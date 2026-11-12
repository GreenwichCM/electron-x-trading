"""ElectronX's trading hours: Monday to Friday, 7:00am to 8:00pm CT, except exchange holidays and early closes."""

from datetime import UTC, date, datetime

import pytest
from trading_core.hours import CHICAGO, ExchangeHours, parse_holidays

HOURS = ExchangeHours()  # the published calendar


def ct(*args):
    return datetime(*args, tzinfo=CHICAGO)


@pytest.mark.parametrize(
    ("now", "is_open", "next_change", "reason"),
    [
        (ct(2026, 11, 11, 10, 0), True, ct(2026, 11, 11, 20, 0), "open"),  # Wednesday morning
        (ct(2026, 11, 11, 6, 59), False, ct(2026, 11, 11, 7, 0), "before the open"),
        (ct(2026, 11, 11, 20, 0), False, ct(2026, 11, 12, 7, 0), "after the close"),
        (ct(2026, 11, 13, 21, 30), False, ct(2026, 11, 16, 7, 0), "after the close"),  # Friday night
        (ct(2026, 11, 14, 12, 0), False, ct(2026, 11, 16, 7, 0), "weekend"),
        (ct(2026, 11, 25, 22, 0), False, ct(2026, 11, 27, 7, 0), "after the close"),  # Thanksgiving is skipped
        (ct(2026, 11, 26, 9, 0), False, ct(2026, 11, 27, 7, 0), "Thanksgiving Day"),
        (ct(2026, 11, 27, 9, 0), True, ct(2026, 11, 27, 13, 0), "early close"),  # closes at 1:00pm CT
        (ct(2026, 11, 27, 13, 30), False, ct(2026, 11, 30, 7, 0), "after the close"),
        (ct(2026, 12, 24, 15, 0), False, ct(2026, 12, 28, 7, 0), "after the close"),  # Christmas, then the weekend
        (ct(2027, 12, 24, 9, 0), False, ct(2027, 12, 27, 7, 0), "Christmas Day (observed)"),
    ],
)
def test_status(now, is_open, next_change, reason):
    status = HOURS.status(now)
    assert (status.open, status.next_change, status.reason) == (is_open, next_change, reason)


def test_central_time_follows_daylight_saving():
    # Clocks went back on Sunday 1 November 2026: 7:00am CT is 12:00 UTC before, 13:00 UTC after
    assert HOURS.status(datetime(2026, 10, 30, 11, 0, tzinfo=UTC)).next_change == datetime(
        2026, 10, 30, 12, 0, tzinfo=UTC
    )
    nov2 = HOURS.status(datetime(2026, 11, 1, 12, 0, tzinfo=UTC)).to_dict()
    assert nov2 == {"open": False, "next_change": "2026-11-02T13:00:00+00:00", "reason": "weekend"}


def test_upcoming_holidays_and_early_closes():
    assert HOURS.upcoming(ct(2026, 11, 12, 9, 0), count=3) == [
        {"date": "2026-11-26", "what": "Thanksgiving Day: closed"},
        {"date": "2026-11-27", "what": "Day after Thanksgiving: closes 1:00pm CT"},
        {"date": "2026-12-24", "what": "Christmas Eve: closes 1:00pm CT"},
    ]


def test_more_holidays_can_be_added():
    extra = ExchangeHours(holidays={date(2028, 1, 3): "set in EXCHANGE_HOLIDAYS"})
    assert extra.status(ct(2028, 1, 3, 9, 0)).reason == "set in EXCHANGE_HOLIDAYS"


def test_describe_and_holidays():
    assert HOURS.describe() == "Monday to Friday, 7:00am to 8:00pm CT"
    assert parse_holidays(" 2028-01-03, 2028-01-17 ,") == {date(2028, 1, 3), date(2028, 1, 17)}
    assert parse_holidays("") == frozenset()
    with pytest.raises(ValueError, match="not a date"):
        parse_holidays("2028-13-01")
