"""Compare the exchange's positions (drop copy) with orderentry's own (from the `orders` stream).

The two can disagree for a moment: the drop copy of a fill may arrive before orderentry has processed
its own report, or the other way round. Only a difference that lasts longer than `grace_s` is a break.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any


class Reconciler:
    def __init__(self, grace_s: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.grace_s = grace_s
        self.clock = clock
        self._orders: dict[str, tuple[str, str, int]] = {}  # ClOrdID -> (symbol, side, filled qty)
        self._since: dict[str, float] = {}  # symbol -> when it first differed

    def on_order(self, order: Mapping[str, Any]) -> None:
        """An update from the orders stream. Filled quantity only grows, so keep the largest seen."""
        cl = order["cl_ord_id"]
        previous = self._orders.get(cl, ("", "", 0))[2]
        self._orders[cl] = (order["symbol"], order["side"], max(previous, int(order.get("cum_qty") or 0)))

    def orderentry_positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for symbol, side, filled in self._orders.values():
            if filled:
                out[symbol] = out.get(symbol, 0) + (filled if side == "BUY" else -filled)
        return out

    def differences(self, exchange: Mapping[str, int]) -> dict[str, tuple[int, int]]:
        """symbol -> (exchange position, orderentry position), for every symbol where they differ."""
        ours = self.orderentry_positions()
        symbols = set(exchange) | set(ours)
        return {s: (exchange.get(s, 0), ours.get(s, 0)) for s in symbols if exchange.get(s, 0) != ours.get(s, 0)}

    def check(self, exchange: Mapping[str, int]) -> str | None:
        """A description of the break if a difference has lasted longer than the grace period, else None."""
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
