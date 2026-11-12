"""When ElectronX is open for trading: Monday to Friday, 7:00am to 8:00pm Central Time, except on exchange
holidays, and until 1:00pm CT on its early-close days.

The hours are ElectronX's own ("Trading Hours" on https://www.electronx.com/products; the rulebook, rule
5.10, says they are published there). The holidays and early closes below are from Exchange Holidays on
https://www.electronx.com/regulatory, as published for 2025 to 2027; EXCHANGE_HOLIDAYS adds more without a
code change. Each contract also has its own times: it stops trading when its delivery hour ends
(LastTradeDate), and its state on market data (OPEN, CLOSED, ...) is what the exchange says now.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

#: Closed all day (ElectronX Exchange Holidays; "obs." is the weekday a weekend holiday is observed on).
HOLIDAYS: dict[date, str] = {
    date(2025, 12, 25): "Christmas Day",
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Presidents' Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Presidents' Day",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
}

#: Open at the usual time, closing early (1:00pm CT / 2:00pm ET).
EARLY_CLOSES: dict[date, tuple[time, str]] = {
    date(2025, 12, 24): (time(13, 0), "Christmas Eve"),
    date(2026, 11, 27): (time(13, 0), "Day after Thanksgiving"),
    date(2026, 12, 24): (time(13, 0), "Christmas Eve"),
    date(2027, 11, 26): (time(13, 0), "Day after Thanksgiving"),
}


def _clock(t: time) -> str:
    return f"{(t.hour - 1) % 12 + 1}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


@dataclass(frozen=True)
class MarketStatus:
    open: bool
    next_change: datetime  # when it closes (if open) or opens next (if closed)
    reason: str  # open, early close, before the open, after the close, weekend, or the holiday's name

    def to_dict(self) -> dict[str, object]:
        return {
            "open": self.open,
            "next_change": self.next_change.astimezone(UTC).isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True, eq=False)
class ExchangeHours:
    opens: time = time(7, 0)
    closes: time = time(20, 0)
    weekdays: frozenset[int] = frozenset(range(5))  # Monday 0 .. Friday 4
    holidays: Mapping[date, str] = field(default_factory=lambda: dict(HOLIDAYS))
    early_closes: Mapping[date, tuple[time, str]] = field(default_factory=lambda: dict(EARLY_CLOSES))
    tz: ZoneInfo = CHICAGO

    def describe(self) -> str:
        return f"Monday to Friday, {_clock(self.opens)} to {_clock(self.closes)} CT"

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() in self.weekdays and day not in self.holidays

    def closes_on(self, day: date) -> time:
        early = self.early_closes.get(day)
        return early[0] if early else self.closes

    def status(self, now: datetime) -> MarketStatus:
        local = now.astimezone(self.tz)
        day = local.date()
        if self.is_trading_day(day):
            opens = datetime.combine(day, self.opens, self.tz)
            closes = datetime.combine(day, self.closes_on(day), self.tz)
            if opens <= local < closes:
                return MarketStatus(True, closes, "early close" if day in self.early_closes else "open")
            if local < opens:
                return MarketStatus(False, opens, "before the open")
            reason = "after the close"
        else:
            reason = self.holidays[day] if day in self.holidays else "weekend"
        nxt = day + timedelta(days=1)
        for _ in range(366):
            if self.is_trading_day(nxt):
                break
            nxt += timedelta(days=1)
        return MarketStatus(False, datetime.combine(nxt, self.opens, self.tz), reason)

    def upcoming(self, now: datetime, count: int = 4) -> list[dict[str, str]]:
        """The next holidays and early closes from today, soonest first."""
        today = now.astimezone(self.tz).date()
        days = sorted(d for d in {*self.holidays, *self.early_closes} if d >= today)[:count]
        out = []
        for d in days:
            if d in self.holidays:
                out.append({"date": d.isoformat(), "what": f"{self.holidays[d]}: closed"})
            else:
                close, name = self.early_closes[d]
                out.append({"date": d.isoformat(), "what": f"{name}: closes {_clock(close)} CT"})
        return out


def parse_holidays(text: str) -> frozenset[date]:
    """EXCHANGE_HOLIDAYS: dates as YYYY-MM-DD, separated by commas. ValueError if one is not a date."""
    out = set()
    for item in text.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.add(date.fromisoformat(item))
        except ValueError:
            raise ValueError(f"EXCHANGE_HOLIDAYS: {item!r} is not a date like 2028-01-03") from None
    return frozenset(out)
