"""The trading day that daily P&L and the daily loss limit run over.

ElectronX publishes no trading day of its own (contracts trade around the clock, each until the end of
its hour), so it is our choice: by default midnight to midnight New York time. A day is named after
the date it starts on, so with a 17:00 start, Tuesday 18:00 is in the day named Tuesday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading_core import NY_TZ


@dataclass(frozen=True)
class TradingDay:
    tz: ZoneInfo = field(default=NY_TZ)
    start: time = time(0)

    @classmethod
    def parse(cls, tz: str, start: str) -> TradingDay:
        """From settings such as ("America/New_York", "00:00"); ValueError if either is wrong."""
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {tz!r}") from exc
        try:
            begins = time.fromisoformat(start)
        except ValueError as exc:
            raise ValueError(f"day start {start!r} is not HH:MM") from exc
        return cls(zone, begins)

    def day_of(self, ts: float) -> date:
        """The trading day that the moment `ts` (seconds since the epoch) falls in."""
        local = datetime.fromtimestamp(ts, self.tz)
        return local.date() - timedelta(days=1) if local.time() < self.start else local.date()

    def starts_at(self, day: date) -> datetime:
        return datetime.combine(day, self.start, self.tz)

    def describe(self, day: date) -> dict[str, str]:
        return {"day": day.isoformat(), "starts_at": self.starts_at(day).isoformat(), "tz": self.tz.key}
