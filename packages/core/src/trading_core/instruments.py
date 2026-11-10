"""ElectronX markets, products, symbols and trading conventions.

Shared by every service and by the research package. No network, database or pandas code here.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

NY_TZ = ZoneInfo("America/New_York")

#: Time zone of the date and hour in a symbol. Assumed to be New York, like the exchange's data;
#: confirm against the ElectronX spec during UAT.
EXPIRY_TZ = NY_TZ


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
    r"-(?P<expiry_date>\d{6})-(?P<expiry_hour>\d{2})$"
)


class Symbol(BaseModel):
    """A parsed contract symbol: ``<PRODUCT>-<MARKET>-<LOCATION>-<YYMMDD>-<HH>``.

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

    @classmethod
    def parse(cls, symbol: str) -> Self:
        m = SYMBOL_RE.match(symbol)
        if m is None:
            raise ValueError(f"Not a valid ElectronX symbol: {symbol!r}")
        g = m.groupdict()
        return cls(
            raw=symbol,
            product=Product(g["product"]),
            product_variant=int(g["variant"]) if g["variant"] else None,
            market=Market(g["market"]),
            location=g["location"],
            expiry_date=datetime.strptime(g["expiry_date"], "%y%m%d").date(),
            expiry_hour=int(g["expiry_hour"]),
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
        """When trading ends: the end of the contract hour, in ``EXPIRY_TZ`` (time-zone aware)."""
        return self.expiry.replace(tzinfo=EXPIRY_TZ)

    def __str__(self) -> str:
        return self.raw
