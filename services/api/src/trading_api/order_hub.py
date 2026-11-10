"""Order update fan-out: reads the Redis streams `orders` (orderentry) and `screen_orders` (dropcopy) and
sends each update only to browsers allowed to see it (the trader's own orders, or everyone's for risk and
management).

Orders entered on the ElectronX screen come only from the drop copy. They carry the trader's ElectronX id
(`exchange_user`), not our trader id, so the hub looks the trader up in the trading rules
(electronx.traders.exchange_trader_id). Their times are FIX timestamps; the hub gives them ISO 8601 times
like orderentry's, so the blotter can sort and show both alike. Our own orders are listed by orderentry;
the hub keeps every order from the screen it has seen (the stream from its start).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from trading_core import TradingRules

from .hub import ClientFeed

log = logging.getLogger("api.orders")

STREAM = "orders"
SCREEN_STREAM = "screen_orders"
LIVE = {"pending_new", "new", "partially_filled", "pending_cancel"}
Order = dict[str, Any]


def iso_time(value: str | None) -> str:
    """A FIX UTCTimestamp (20261110-14:03:56.123456789) as ISO 8601; anything else unchanged."""
    if not value:
        return ""
    whole, _, fraction = value.partition(".")
    try:
        when = datetime.strptime(f"{whole}.{fraction[:6] or '0'}", "%Y%m%d-%H:%M:%S.%f")
    except ValueError:
        return value
    return when.replace(tzinfo=UTC).isoformat(timespec="milliseconds")


class OrderHub:
    RETRY_S = 1.0

    def __init__(self, redis: Redis):
        self.redis = redis
        self.clients: dict[ClientFeed, Callable[[Order], bool]] = {}
        self.ready = asyncio.Event()  # set once reading has started
        self.rules: TradingRules | None = None
        self._screen: dict[str, Order] = {}  # cl_ord_id -> the latest update from dropcopy, as published

    def connect(self, wants: Callable[[Order], bool]) -> ClientFeed:
        feed = ClientFeed()
        self.clients[feed] = wants
        return feed

    def disconnect(self, feed: ClientFeed) -> None:
        self.clients.pop(feed, None)

    def set_rules(self, rules: TradingRules) -> None:
        """The trading rules, for who each ElectronX id belongs to."""
        self.rules = rules

    def screen_row(self, raw: Order) -> Order:
        """An order from the ElectronX screen in the shape of orderentry's orders."""
        trader = self.rules.trader_for(raw.get("exchange_user")) if self.rules else None
        status = raw.get("status", "")
        return {
            **raw,
            "trader": trader or "",
            "source": "electronx",
            "created": iso_time(raw.get("created")),
            "updated": iso_time(raw.get("updated")),
            "leaves": max(0, int(raw.get("qty", 0)) - int(raw.get("cum_qty", 0))) if status in LIVE else 0,
        }

    def screen_orders(self, trader: str | None = None) -> list[Order]:
        """Orders from the ElectronX screen: all of them, or one trader's."""
        rows = [self.screen_row(raw) for raw in self._screen.values()]
        return [r for r in rows if trader is None or r["trader"] == trader]

    def _keep(self, order: Order) -> bool:
        """Keep an update from the screen unless a newer one of the same order is already here."""
        held = self._screen.get(order["cl_ord_id"])
        if held is not None and int(held.get("version", 0)) > int(order.get("version", 0)):
            return False
        self._screen[order["cl_ord_id"]] = order
        return True

    async def run(self) -> None:
        """Read `orders` from its current end and `screen_orders` from its start, then every new entry,
        resuming after Redis outages.

        The positions are kept as real entry ids, not "$": after a reconnect, reading continues exactly
        after the last entry seen, so no update is skipped.
        """
        last: dict[str, bytes | str] = {}
        while True:
            try:
                if not last:
                    newest = await self.redis.xrevrange(STREAM, count=1)
                    last = {STREAM: newest[0][0] if newest else "0-0", SCREEN_STREAM: "0-0"}
                self.ready.set()
                for stream, entries in await self.redis.xread(dict(last), block=1000, count=500) or []:
                    name = stream.decode() if isinstance(stream, bytes) else stream
                    for entry_id, fields in entries:
                        last[name] = entry_id
                        order = json.loads(fields[b"data"] if b"data" in fields else fields["data"])
                        if name == SCREEN_STREAM:
                            if not self._keep(order):
                                continue
                            order = self.screen_row(order)
                        for feed, wants in list(self.clients.items()):
                            if wants(order):
                                feed.put(order["cl_ord_id"], order)
            except (RedisError, OSError) as exc:
                self.ready.clear()
                log.warning("reading the order streams failed (%s); retrying in %.0fs", exc, self.RETRY_S)
                await asyncio.sleep(self.RETRY_S)
