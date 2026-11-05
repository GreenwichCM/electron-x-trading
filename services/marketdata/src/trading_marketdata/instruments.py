"""Instrument reference data from SecurityList [y]: tick size, lot size and price limits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Instrument:
    symbol: str
    tick_size: float | None = None  # MinPriceIncrement(969)
    lot_size: int = 1  # MinTradeVol(562)
    low_limit: float | None = None  # LowLimitPrice(1148)
    high_limit: float | None = None  # HighLimitPrice(1149)
    state: str = ""  # TradingSessionID(336) seen on market data, e.g. OPEN

    @classmethod
    def from_security_list(cls, entry: dict[int, str]) -> Instrument:
        def num(tag: int) -> float | None:
            return float(entry[tag]) if entry.get(tag) else None

        return cls(
            symbol=entry[55],
            tick_size=num(969),
            lot_size=int(num(562) or 1),
            low_limit=num(1148),
            high_limit=num(1149),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
