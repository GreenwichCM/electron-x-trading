"""Instrument reference data from SecurityList [y]: tick size, lot size, price limits and trading times."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

#: EventText(868) of the SecurityList events that carry a time, and where each one is kept.
EVENT_TIMES = {"LastTradeDate": "last_trade_time", "ExpirationDate": "expiration_time"}


def utc_iso(value: str) -> str | None:
    """A FIX UTCTimestamp such as 20251123-07:00:00.000000000 as ISO 8601, to the second."""
    try:
        return datetime.strptime(value.partition(".")[0], "%Y%m%d-%H:%M:%S").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


@dataclass
class Instrument:
    symbol: str
    tick_size: float | None = None  # MinPriceIncrement(969)
    lot_size: int = 1  # MinTradeVol(562)
    low_limit: float | None = None  # LowLimitPrice(1148)
    high_limit: float | None = None  # HighLimitPrice(1149)
    last_trade_time: str | None = None  # EventTime(1145) of the LastTradeDate event: trading stops
    expiration_time: str | None = None  # EventTime(1145) of the ExpirationDate event
    state: str = ""  # TradingSessionID(336) seen on market data, e.g. OPEN or EXPIRED

    @classmethod
    def from_security_list(cls, fields: list[tuple[int, str]]) -> Instrument:
        """One entry of the SecurityList group, its fields in order (the events are a nested group)."""
        entry = dict(fields)

        def num(tag: int) -> float | None:
            return float(entry[tag]) if entry.get(tag) else None

        inst = cls(
            symbol=entry[55],
            tick_size=num(969),
            lot_size=int(num(562) or 1),
            low_limit=num(1148),
            high_limit=num(1149),
        )
        events: list[dict[int, str]] = []
        for tag, value in fields:
            if tag == 865:  # EventType starts each event
                events.append({})
            elif events and tag in (866, 868, 1145):
                events[-1][tag] = value
        for event in events:
            attr = EVENT_TIMES.get(event.get(868, ""))
            if attr and event.get(1145):
                setattr(inst, attr, utc_iso(event[1145]))
        return inst

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
