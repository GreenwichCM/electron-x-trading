"""The drop copy session app: every ExecutionReport the exchange sends for the account.

The drop copy carries every order of the account, whether it was sent by our orderentry or entered on
the ElectronX screen, each with the trader's ElectronX id in TargetSubID(57). Fills build the exchange's
positions; all reports together keep one record per exchange order (ExchangeOrder), so orders from the
screen can be recorded too (see reconcile.py for how ours and theirs are told apart).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from trading_fix import FixMessage, FixSession

from .journal import FillJournal

log = logging.getLogger("dc")

SIDES = {"1": "BUY", "2": "SELL"}
TIFS = {"0": "DAY", "1": "GTC", "3": "IOC", "4": "FOK", "6": "GTD"}
#: OrdStatus(39), in the words orderentry uses
STATUSES = {
    "0": "new",
    "1": "partially_filled",
    "2": "filled",
    "3": "done_for_day",
    "4": "canceled",
    "8": "rejected",
    "A": "pending_new",
    "C": "expired",
}


class FillSink(Protocol):
    def fill(self, fill: dict[str, Any]) -> None: ...
    def positions(self, positions: dict[str, int], connected: bool) -> None: ...


class MemoryFillSink:
    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []
        self.latest_positions: dict[str, int] = {}
        self.connected = False

    def fill(self, fill: dict[str, Any]) -> None:
        self.fills.append(fill)

    def positions(self, positions: dict[str, int], connected: bool) -> None:
        self.latest_positions = dict(positions)
        self.connected = connected


@dataclass
class ExchangeOrder:
    """One order at the exchange, as the drop copy has seen it so far."""

    order_id: str  # the exchange's OrderID(37)
    cl_ord_id: str  # the first ClOrdID seen; an amend or cancel adds more
    user: str  # TargetSubID(57): the trader's ElectronX id
    symbol: str
    side: str = ""
    qty: int = 0
    price: float = 0.0
    tif: str = ""
    status: str = ""
    cum_qty: int = 0
    avg_px: float = 0.0
    text: str = ""
    manual: bool = False  # ManualOrderIndicator(1028)
    created: str = ""
    updated: str = ""
    filled: int = 0  # signed lots from the fills counted here
    cl_ord_ids: set[str] = field(default_factory=set)
    first_seen: float = 0.0  # monotonic clock; 0 for orders rebuilt from the journal at startup
    version: int = 0  # +1 on every report
    published: int = 0  # the version last published as an order from the screen

    def to_dict(self) -> dict[str, Any]:
        """In the shape of orderentry's order updates, so the recorder can store both alike."""
        return {
            "cl_ord_id": self.cl_ord_id,
            "order_id": self.order_id,
            "trader": "",  # the recorder looks it up from exchange_user
            "exchange_user": self.user,
            "source": "electronx",
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "tif": self.tif,
            "status": self.status,
            "cum_qty": self.cum_qty,
            "avg_px": self.avg_px,
            "text": self.text,
            "manual": self.manual,
            "created": self.created,
            "updated": self.updated,
            "version": self.version,
        }


class DropCopyApp:
    """Positions per symbol for the whole account, built only from the exchange's drop copy."""

    def __init__(self, sink: FillSink, journal: FillJournal | None = None, clock: Callable[[], float] = time.monotonic):
        self.sink = sink
        self.journal = journal
        self.clock = clock
        self.positions: dict[str, int] = {}
        self.orders: dict[str, ExchangeOrder] = {}  # OrderID -> order
        self.seen: set[str] = set()  # ExecIDs already counted
        self.connected = False
        if journal is not None:
            replayed = journal.load()
            for fill in replayed:
                self._apply(fill, first_seen=0.0)
            log.info("replayed %d fills from %s", len(replayed), journal.path)

    # ---- SessionApp ------------------------------------------------------------------------

    def on_logon(self, session: FixSession) -> None:
        self.connected = True
        log.info("drop copy logged on")
        self.sink.positions(self.positions, True)

    def on_disconnect(self, session: FixSession) -> None:
        self.connected = False
        log.warning("drop copy down: positions are not being updated")
        self.sink.positions(self.positions, False)

    def on_app_message(self, session: FixSession, msg: FixMessage) -> None:
        if msg.msg_type != "8":
            return
        exec_id = msg.get(17, "")
        if not exec_id or exec_id in self.seen:
            return  # a resent copy of a report we already have
        if msg.get(150) != "F":
            self.seen.add(exec_id)
            self._track(msg)
            return
        fill = {
            "exec_id": exec_id,
            "order_id": msg.get(37, ""),
            "cl_ord_id": msg.get(11, ""),
            "account": msg.get(1, ""),
            "user": msg.get(57, ""),
            "manual": msg.get(1028) == "Y",
            "symbol": msg.get(55, ""),
            "side": SIDES.get(msg.get(54, ""), ""),
            "qty": msg.get_int(32),
            "price": msg.get_float(31),
            "time": msg.get(60, ""),
        }
        if self.journal is not None:
            self.journal.append(fill)
        self._apply(fill, first_seen=self.clock())
        self._track(msg)
        log.info(
            "fill %s %s %d %s @ %s (%s)",
            fill["exec_id"],
            fill["side"],
            fill["qty"],
            fill["symbol"],
            fill["price"],
            fill["user"] or "no user",
        )
        self.sink.fill(fill)
        self.sink.positions(self.positions, self.connected)

    # ---- state -----------------------------------------------------------------------------

    def _order(self, order_id: str, cl_ord_id: str, user: str, symbol: str, first_seen: float) -> ExchangeOrder:
        order = self.orders.get(order_id)
        if order is None:
            order = ExchangeOrder(order_id, cl_ord_id, user, symbol, first_seen=first_seen)
            self.orders[order_id] = order
        if cl_ord_id:
            order.cl_ord_ids.add(cl_ord_id)
        return order

    def _apply(self, fill: dict[str, Any], first_seen: float) -> None:
        self.seen.add(fill["exec_id"])
        signed = int(fill["qty"]) if fill["side"] == "BUY" else -int(fill["qty"])
        self.positions[fill["symbol"]] = self.positions.get(fill["symbol"], 0) + signed
        key = fill.get("order_id") or f"cl:{fill.get('cl_ord_id', '')}"
        order = self._order(key, fill.get("cl_ord_id", ""), fill.get("user", ""), fill["symbol"], first_seen)
        order.filled += signed
        order.side = order.side or fill["side"]

    def _track(self, msg: FixMessage) -> None:
        """Keep the order's record up to date from any report (a rejected order has no OrderID: skipped)."""
        order_id = msg.get(37, "")
        if not order_id or order_id == "NONE":
            return
        order = self._order(order_id, msg.get(11, ""), msg.get(57, ""), msg.get(55, ""), self.clock())
        order.side = SIDES.get(msg.get(54, ""), order.side)
        order.qty = msg.get_int(38, order.qty)
        order.price = msg.get_float(44, order.price)
        order.tif = TIFS.get(msg.get(59, ""), order.tif)
        order.status = STATUSES.get(msg.get(39, ""), order.status)
        order.cum_qty = msg.get_int(14, order.cum_qty)
        order.avg_px = msg.get_float(6, order.avg_px)
        order.text = msg.get(58, "") or order.text
        order.manual = msg.get(1028) == "Y"
        order.updated = msg.get(60, "") or order.updated
        order.created = order.created or order.updated
        order.version += 1
