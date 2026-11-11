"""Compare the exchange's positions (drop copy) with orderentry's own (from the `orders` stream).

The drop copy has every order of the account, including those traders enter on the ElectronX screen.
An exchange order is ours if orderentry published it: its ClOrdID or OrderID is in the `orders` stream.
Only our orders are compared with orderentry, so screen trading is never a break. An order nobody here
knows is counted as from the screen once it has been unknown for `screen_after_s` (orderentry's own
update can reach Redis a moment after the drop copy).

The two can also disagree for a moment about our own orders: the drop copy of a fill may arrive before
orderentry has processed its own report, or the other way round. Only a difference that lasts longer
than `grace_s` is a break.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .app import ExchangeOrder


@dataclass
class Split:
    ours: dict[str, int] = field(default_factory=dict)  # symbol -> lots, orders orderentry sent
    screen: dict[str, int] = field(default_factory=dict)  # symbol -> lots, orders from the ElectronX screen
    screen_by_user: dict[str, dict[str, int]] = field(default_factory=dict)  # ElectronX user -> symbol -> lots
    pending: dict[str, int] = field(default_factory=dict)  # symbol -> lots, orders not known yet: either
    screen_orders: list[ExchangeOrder] = field(default_factory=list)

    def published(self) -> dict[str, dict]:
        """What goes into dc:positions next to the exchange's positions."""
        return {"screen": self.screen, "screen_by_user": self.screen_by_user, "pending": self.pending}


def _add(into: dict[str, int], symbol: str, qty: int) -> None:
    into[symbol] = into.get(symbol, 0) + qty
    if into[symbol] == 0:
        del into[symbol]


class Reconciler:
    def __init__(self, grace_s: float = 5.0, screen_after_s: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.grace_s = grace_s
        self.screen_after_s = screen_after_s
        self.clock = clock
        # ClOrdID -> (symbol, side, filled qty less busted, version of the update it is from)
        self._orders: dict[str, tuple[str, str, int, int]] = {}
        self._order_ids: set[str] = set()  # the exchange's OrderIDs of orderentry's orders
        self._since: dict[str, float] = {}  # symbol -> when it first differed

    def on_order(self, order: Mapping[str, Any]) -> None:
        """An update from the orders stream; an older version than the one held changes nothing. What
        counts is the quantity filled, less any of it the exchange has busted since. Updates without a
        version (from before step 10) can only add fills, so of those the largest wins."""
        cl = order["cl_ord_id"]
        version = int(order.get("version") or 0)
        net = int(order.get("cum_qty") or 0) - int(order.get("busted_qty") or 0)
        previous = self._orders.get(cl)
        if previous is not None:
            if version < previous[3]:
                return
            if version == previous[3]:
                net = max(net, previous[2])
        self._orders[cl] = (order["symbol"], order["side"], net, version)
        if order.get("order_id"):
            self._order_ids.add(order["order_id"])

    def is_ours(self, order: ExchangeOrder) -> bool:
        return order.order_id in self._order_ids or any(cl in self._orders for cl in order.cl_ord_ids)

    def split(self, orders: Iterable[ExchangeOrder]) -> Split:
        """The exchange's orders divided into ours and the screen's; one not known yet is pending."""
        now = self.clock()
        out = Split()
        for order in orders:
            if self.is_ours(order):
                _add(out.ours, order.symbol, order.filled)
            elif now - order.first_seen >= self.screen_after_s:
                out.screen_orders.append(order)
                if order.filled:
                    _add(out.screen, order.symbol, order.filled)
                    _add(out.screen_by_user.setdefault(order.user, {}), order.symbol, order.filled)
            elif order.filled:
                _add(out.pending, order.symbol, order.filled)
        return out

    def orderentry_positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for symbol, side, filled, _version in self._orders.values():
            if filled:
                out[symbol] = out.get(symbol, 0) + (filled if side == "BUY" else -filled)
        return out

    def differences(self, exchange: Mapping[str, int]) -> dict[str, tuple[int, int]]:
        """symbol -> (exchange position, orderentry position), for every symbol where they differ."""
        ours = self.orderentry_positions()
        symbols = set(exchange) | set(ours)
        return {s: (exchange.get(s, 0), ours.get(s, 0)) for s in symbols if exchange.get(s, 0) != ours.get(s, 0)}

    def check(self, exchange: Mapping[str, int]) -> str | None:
        """A description of the break if a difference has lasted longer than the grace period, else None.
        `exchange` is the exchange's position in orderentry's orders only (Split.ours)."""
        now = self.clock()
        diffs = self.differences(exchange)
        for symbol in list(self._since):
            if symbol not in diffs:
                del self._since[symbol]
        broken = []
        for symbol, (theirs, ours) in sorted(diffs.items()):
            since = self._since.setdefault(symbol, now)
            if now - since >= self.grace_s:
                broken.append(f"{symbol}: exchange {theirs:+d}, orderentry {ours:+d}")
        return "position break on " + "; ".join(broken) if broken else None
