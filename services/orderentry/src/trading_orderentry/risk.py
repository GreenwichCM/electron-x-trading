"""Pre-trade risk: every order is checked here before it can reach the exchange.

Entitlements and the limits used here come from the trading rules in SQL Server (see
trading_core/rules.py); OrderEntryApp turns them into RiskLimits for each order.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .orders import Order


@dataclass(frozen=True)
class RiskLimits:
    max_order_qty: int = 10
    max_order_notional: float = 5_000.0
    max_open_orders: int = 20  # per trader
    max_position: int = 50  # per trader and contract, counting resting orders (worst case)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: float
    lot_size: int = 1
    low_limit: float | None = None
    high_limit: float | None = None
    state: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Instrument:
        return cls(
            symbol=d["symbol"],
            tick_size=float(d.get("tick_size") or 0.25),
            lot_size=int(d.get("lot_size") or 1),
            low_limit=d.get("low_limit"),
            high_limit=d.get("high_limit"),
            state=d.get("state") or "",
        )

    def on_tick(self, price: float) -> bool:
        ticks = price / self.tick_size
        return abs(ticks - round(ticks)) < 1e-9


def check_order(
    order: Order,
    instrument: Instrument | None,
    limits: RiskLimits,
    trader_open_orders: Iterable[Order],
    position: int,
) -> str | None:
    """The reason to reject `order`, or None if it may go to the exchange."""
    if instrument is None:
        return f"unknown instrument {order.symbol}"
    if instrument.state and instrument.state != "OPEN":
        return f"{order.symbol} is {instrument.state}"
    if order.qty <= 0:
        return "quantity must be positive"
    if order.qty % instrument.lot_size:
        return f"quantity must be a multiple of {instrument.lot_size}"
    if order.qty > limits.max_order_qty:
        return f"quantity {order.qty} is above the limit of {limits.max_order_qty}"
    if not instrument.on_tick(order.price):
        return f"price {order.price} is not on the {instrument.tick_size} tick"
    if instrument.low_limit is not None and order.price < instrument.low_limit:
        return f"price is below the exchange limit {instrument.low_limit}"
    if instrument.high_limit is not None and order.price > instrument.high_limit:
        return f"price is above the exchange limit {instrument.high_limit}"
    if order.qty * order.price > limits.max_order_notional:
        return f"order value {order.qty * order.price:.2f} is above the limit of {limits.max_order_notional:.2f}"
    open_orders = list(trader_open_orders)
    if len(open_orders) >= limits.max_open_orders:
        return f"already {len(open_orders)} open orders (limit {limits.max_open_orders})"
    same = [o for o in open_orders if o.symbol == order.symbol]
    if order.side == "BUY":
        worst = position + sum(o.leaves for o in same if o.side == "BUY") + order.qty
        if worst > limits.max_position:
            return f"position could reach {worst} (limit {limits.max_position})"
    else:
        worst = position - sum(o.leaves for o in same if o.side == "SELL") - order.qty
        if worst < -limits.max_position:
            return f"position could reach {worst} (limit -{limits.max_position})"
    return None


class Throttle:
    """Counts messages over a sliding window. New orders may use only part of the budget, so there is
    always room left to cancel (ElectronX allows 100 messages per second on the order entry session)."""

    def __init__(self, limit: int, window_s: float = 1.0, cancel_reserve: float = 0.25):
        self.limit = limit
        self.window_s = window_s
        self.new_limit = max(1, int(limit * (1 - cancel_reserve)))
        self._sent: deque[float] = deque()

    def _count(self, now: float) -> int:
        while self._sent and now - self._sent[0] >= self.window_s:
            self._sent.popleft()
        return len(self._sent)

    def allow(self, is_cancel: bool = False) -> bool:
        now = time.monotonic()
        if self._count(now) >= (self.limit if is_cancel else self.new_limit):
            return False
        self._sent.append(now)
        return True
