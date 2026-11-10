"""ElectronX markets, products, symbols and trading conventions.

Shared by every service and by the research package. No network, database or pandas code here.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

NY_TZ = ZoneInfo("America/New_York")


class Market(StrEnum):
    ERCOT = "ERCOT"
    PJM = "PJM"
    MISO = "MISO"
    CAISO = "CAISO"
    SPP = "SPP"
    ISO_NE = "ISO-NE"
    NYISO = "NYISO"


class Product(StrEnum):
    BFUT = "BFUT"
    BOPT = "BOPT"


class AggressorSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


#: Each ISO's main time zone. A symbol's date and hour ending are local to it (ElectronX FIX
#: specification, Symbology); MISO stays on EST all year.
MARKET_TZ: dict[Market, ZoneInfo] = {
    Market.ERCOT: ZoneInfo("America/Chicago"),
    Market.PJM: NY_TZ,
    Market.MISO: ZoneInfo("Etc/GMT+5"),
    Market.CAISO: ZoneInfo("America/Los_Angeles"),
    Market.SPP: ZoneInfo("America/Chicago"),
    Market.ISO_NE: NY_TZ,
    Market.NYISO: NY_TZ,
}

#: First date data exists for each market.
MARKET_LAUNCH_DATES: dict[Market, date] = {
    Market.ERCOT: date(2025, 12, 10),
    Market.PJM: date(2026, 4, 6),
    Market.CAISO: date(2026, 6, 1),
    Market.MISO: date(2026, 6, 1),
    Market.SPP: date(2026, 8, 24),
    Market.ISO_NE: date(2026, 8, 24),
    Market.NYISO: date(2026, 8, 24),
}

#: Earliest date with any data, across all markets.
DATA_START_DATE: date = min(MARKET_LAUNCH_DATES.values())

#: Fallback only; prefer each instrument's own tick size from the exchange.
TICK_SIZE = 0.25  # USD / MWh
LOT_SIZE_MWH = 1.0  # 1 lot = 1 contract = 1 MWh


_MARKET_ALT = "|".join(re.escape(m.value) for m in sorted(Market, key=lambda m: -len(m.value)))
SYMBOL_RE = re.compile(
    rf"^(?P<product>{'|'.join(p.value for p in Product)})(?P<variant>\d*)"
    rf"-(?P<market>{_MARKET_ALT})"
    r"-(?P<location>.+)"
    r"-(?P<expiry_date>\d{6})-(?P<expiry_hour>\d{2}|X\d)$"
)


class Symbol(BaseModel):
    """A parsed contract symbol: ``<PRODUCT>-<MARKET>-<LOCATION>-<YYMMDD>-<HH>``.

    The date and hour ending are in the ISO's time zone (MARKET_TZ). When clocks fall back, the repeated
    hour has the code ``X2`` instead of ``02``.

    >>> Symbol.parse("BFUT500-ERCOT-HB_NORTH-260701-18").market
    <Market.ERCOT: 'ERCOT'>
    """

    model_config = ConfigDict(frozen=True)

    raw: str
    product: Product
    product_variant: int | None = Field(None, description="Numeric suffix on the product code, e.g. 500 in BFUT500.")
    market: Market
    location: str = Field(description="Hub / zone, e.g. HB_NORTH, ZONE_A.")
    expiry_date: date
    expiry_hour: int = Field(ge=0, le=24, description="HH part of the expiry (hour ending).")
    repeated_hour: bool = Field(False, description="X2: the second 01:00-02:00 when clocks fall back.")

    @classmethod
    def parse(cls, symbol: str) -> Self:
        m = SYMBOL_RE.match(symbol)
        if m is None:
            raise ValueError(f"Not a valid ElectronX symbol: {symbol!r}")
        g = m.groupdict()
        repeated = g["expiry_hour"].startswith("X")
        return cls(
            raw=symbol,
            product=Product(g["product"]),
            product_variant=int(g["variant"]) if g["variant"] else None,
            market=Market(g["market"]),
            location=g["location"],
            expiry_date=datetime.strptime(g["expiry_date"], "%y%m%d").date(),
            expiry_hour=int(g["expiry_hour"][1:] if repeated else g["expiry_hour"]),
            repeated_hour=repeated,
        )

    @property
    def product_code(self) -> str:
        """Product with its variant suffix, e.g. ``BFUT500``."""
        return f"{self.product.value}{self.product_variant or ''}"

    @property
    def expiry(self) -> datetime:
        """Expiry as a naive datetime (``expiry_hour`` 24 rolls to next day 00:00)."""
        return datetime.combine(self.expiry_date, datetime.min.time()) + timedelta(hours=self.expiry_hour)

    @property
    def expires_at(self) -> datetime:
        """The end of the contract hour (in UTC), worked out from the symbol in the ISO's time zone.

        Prefer the exchange's own times from SecurityList where there are any: trading can stop before
        this (LastTradeDate)."""
        tz = MARKET_TZ[self.market]
        start = datetime.combine(self.expiry_date, datetime.min.time()) + timedelta(hours=self.expiry_hour - 1)
        start = start.replace(tzinfo=tz, fold=int(self.repeated_hour))  # fold=1: the second 01:00
        return start.astimezone(UTC) + timedelta(hours=1)  # in UTC: a repeated local hour compares wrongly

    def __str__(self) -> str:
        return self.raw
