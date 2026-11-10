"""A full, rolling universe for the simulator: every hour of today and tomorrow, for each hub and product.

Like the exchange, contracts are named for their delivery date and hour ending in the ISO's time zone.
Each stops trading when its delivery hour begins, expires at the end of the hour and settles a few
minutes later; a settled contract leaves the list an hour after that. roll() lists the next day's
contracts as days go by.
"""

from __future__ import annotations

import logging
import math
import random
import time
from datetime import date, datetime, timedelta

from trading_core import MARKET_TZ, Market, Symbol

from .marketdata import SimInstrument, SimMarketData

log = logging.getLogger("sim.universe")

HUBS: dict[Market, tuple[str, ...]] = {
    Market.ERCOT: ("HB_NORTH", "HB_WEST", "HB_HOUSTON", "HB_SOUTH", "HB_PAN", "HB_BUSAVG", "HB_HUBAVG", "HB_PILOT"),
    Market.PJM: ("WESTERN_HUB",),
}
PRODUCTS = ("BFUT100", "BFUT500", "BFUT5000", "BOPT")
SETTLES_AFTER_S = 300  # after the delivery hour ends
DELIST_AFTER_S = 3600  # after settling


def _fair(hub: str, hour: int, product: str, rng: random.Random) -> float:
    """A plausible price: a daily shape peaking in the late afternoon, different for each hub."""
    if product == "BOPT":
        return float(rng.randint(30, 70))  # a binary option pays 0 or 100
    base = 25 + (sum(map(ord, hub)) % 20)
    return round(base + 20 * max(0.0, math.sin((hour - 6) / 24 * 2 * math.pi)) + rng.uniform(-2, 2), 0)


def day_contracts(market: Market, day: date, rng: random.Random, after: float = 0.0) -> list[SimInstrument]:
    """Every hour of `day` (in the market's time zone) still trading at `after`."""
    out = []
    for hub in HUBS[market]:
        for product in PRODUCTS:
            for hour in range(1, 25):
                symbol = f"{product}-{market.value}-{hub}-{day:%y%m%d}-{hour:02d}"
                ends = Symbol.parse(symbol).expires_at.timestamp()
                last_trade = ends - 3600  # trading stops when the delivery hour begins
                if last_trade <= after:
                    continue
                cap = 100.0 if product in ("BOPT", "BFUT100") else float(product.removeprefix("BFUT"))
                out.append(
                    SimInstrument(
                        symbol,
                        fair=min(_fair(hub, hour, product, rng), cap - 5),
                        high=cap,
                        last_trade_at=last_trade,
                        expires_at=ends,
                        settle_after_s=3600 + SETTLES_AFTER_S,
                    )
                )
    return out


def full_universe(now: float, rng: random.Random) -> list[SimInstrument]:
    """Today's contracts still trading, and all of tomorrow's, in every market."""
    out = []
    for market in HUBS:
        today = datetime.fromtimestamp(now, MARKET_TZ[market]).date()
        for day in (today, today + timedelta(days=1)):
            out += day_contracts(market, day, rng, after=now)
    return out


def roll(md: SimMarketData, rng: random.Random, now: float | None = None) -> tuple[int, int]:
    """List contracts for days that have come into range, and delist long-settled ones.
    Returns (listed, delisted)."""
    now = time.time() if now is None else now
    listed = 0
    for inst in full_universe(now, rng):
        if inst.symbol not in md.instruments:
            md.list_instrument(inst)
            listed += 1
    gone = [
        s
        for s, i in md.instruments.items()
        if i.settlement is not None
        and i.last_trade_at is not None
        and now >= i.last_trade_at + i.settle_after_s + DELIST_AFTER_S
    ]
    for symbol in gone:
        md.delist(symbol)
    if listed or gone:
        log.info(
            "listed %d new contracts, delisted %d settled ones (%d in all)", listed, len(gone), len(md.instruments)
        )
    return listed, len(gone)
