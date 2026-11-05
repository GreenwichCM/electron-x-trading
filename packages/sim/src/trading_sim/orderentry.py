"""Order entry side of the simulated gateway: limit orders, cancels, matching and ExecutionReports.

Follows the ElectronX rules the OMS depends on:
  * NewOrderSingle [D] and OrderCancelRequest [F] need SenderSubID(50) (the trader) in the header
  * limit orders only (OrdType 40=2), TimeInForce DAY(0), GTC(1), IOC(3) or FOK(4)
  * price on the tick and inside the instrument's limits, instrument must be OPEN
  * price-time matching against everything resting in the shared book
  * rejects come back as ExecutionReport 150=8, unknown cancels as OrderCancelReject [9]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from trading_fix import FixMessage, utc_timestamp

from .gateway import Fields, SimGateway, SimSession
from .marketdata import BID, BUY, OFFER, SELL, RestingOrder, SimMarketData, business_reject

log = logging.getLogger("sim.oe")

TIFS = {"0", "1", "3", "4"}  # DAY, GTC, IOC, FOK
NEW, PARTIAL, FILLED, CANCELED, REJECTED = "0", "1", "2", "4", "8"  # OrdStatus(39)
EXEC_NEW, EXEC_CANCELED, EXEC_REJECTED, EXEC_TRADE = "0", "4", "8", "F"  # ExecType(150)


@dataclass(eq=False)
class ClientOrder:
    order_id: str
    cl_ord_id: str
    sender: str
    trader: str
    account: str
    symbol: str
    side: str  # BUY or SELL
    qty: int
    ticks: int
    tif: str
    cum_qty: int = 0
    notional_ticks: int = 0
    status: str = NEW

    @property
    def leaves(self) -> int:
        return self.qty - self.cum_qty if self.status in (NEW, PARTIAL) else 0


class SimOrderEntry:
    """Attach to the order entry SimGateway; shares the book of `md`."""

    def __init__(self, gateway: SimGateway, md: SimMarketData):
        self.gateway = gateway
        self.md = md
        gateway.on_app_message = self._on_message
        md.on_fill = self._on_resting_fill
        self.orders: dict[str, ClientOrder] = {}  # OrderID -> order
        self.by_cl_ord_id: dict[tuple[str, str], ClientOrder] = {}  # (SenderCompID, ClOrdID) -> order

    # ---- requests --------------------------------------------------------------------------

    def _on_message(self, session: SimSession, msg: FixMessage) -> None:
        if msg.msg_type in ("D", "F") and not msg.get(50):
            business_reject(session, msg, 5, "SenderSubID(50) required on order messages")
        elif msg.msg_type == "D":
            self._new_order(session, msg)
        elif msg.msg_type == "F":
            self._cancel(session, msg)
        else:
            business_reject(session, msg, 3, "Unsupported Message Type")
        self.md.flush()

    def _new_order(self, session: SimSession, msg: FixMessage) -> None:
        cl = msg.get(11, "")
        symbol = msg.get(55, "")
        inst = self.md.instruments.get(symbol)
        try:
            qty = int(msg.get(38) or "0")
            price = float(msg.get(44) or "nan")
        except ValueError:
            qty, price = 0, float("nan")
        side, tif = msg.get(54, ""), msg.get(59, "0")
        ticks = round(price / inst.tick) if inst and price == price else 0
        order = ClientOrder(
            order_id=self.md.new_id("O"),
            cl_ord_id=cl,
            sender=session.sender,
            trader=msg.get(50, ""),
            account=msg.get(1, ""),
            symbol=symbol,
            side=side,
            qty=qty,
            ticks=ticks,
            tif=tif,
        )

        reason = ""
        if (session.sender, cl) in self.by_cl_ord_id or not cl:
            reason = "Duplicate or missing ClOrdID"
        elif inst is None:
            reason = f"Unknown security {symbol}"
        elif inst.state != "OPEN":
            reason = f"Book is {inst.state}"
        elif msg.get(40) != "2":
            reason = "Only limit orders (OrdType 2) are supported"
        elif side not in (BUY, SELL):
            reason = "Invalid Side"
        elif tif not in TIFS:
            reason = "Unsupported TimeInForce"
        elif qty <= 0 or qty % inst.lot:
            reason = "Invalid OrderQty"
        elif price != price or abs(price / inst.tick - ticks) > 1e-9:
            reason = "Price is not on the tick"
        elif not inst.low <= price <= inst.high:
            reason = f"Price outside limits {inst.low:.2f}..{inst.high:.2f}"
        if reason:
            order.status = REJECTED
            self._report(session, order, EXEC_REJECTED, text=reason)
            return

        self.orders[order.order_id] = order
        self.by_cl_ord_id[(session.sender, cl)] = order
        self._report(session, order, EXEC_NEW)

        if tif == "4" and self._available(order) < qty:  # FOK: all or nothing
            order.status = CANCELED
            self._report(session, order, EXEC_CANCELED, text="FOK could not be filled in full")
            return
        self._match(order)
        if order.leaves and tif in ("3", "4"):
            order.status = CANCELED
            self._report(session, order, EXEC_CANCELED)
        elif order.leaves:
            book_side = BID if side == BUY else OFFER
            self.md.add_order(symbol, book_side, ticks, order.leaves, owner=session.sender, order_id=order.order_id)

    def _cancel(self, session: SimSession, msg: FixMessage) -> None:
        cl, orig = msg.get(11, ""), msg.get(41, "")
        order = self.by_cl_ord_id.get((session.sender, orig))
        if order is None or not order.leaves:
            status = order.status if order else REJECTED
            session.send(
                "9",
                [
                    (11, cl),
                    (41, orig),
                    (37, order.order_id if order else "NONE"),
                    (39, status),
                    (434, "1"),
                    (102, "1" if order is None else "0"),
                    (58, "Unknown order" if order is None else "Order is not open"),
                ],
            )
            return
        if order.order_id in self.md.books[order.symbol]:
            self.md.cancel_order(order.symbol, order.order_id)
        order.status = CANCELED
        self.by_cl_ord_id[(session.sender, cl)] = order
        self._report(session, order, EXEC_CANCELED, cl_ord_id=cl, orig_cl_ord_id=orig)

    # ---- matching --------------------------------------------------------------------------

    def _crosses(self, order: ClientOrder, ticks: int) -> bool:
        return ticks <= order.ticks if order.side == BUY else ticks >= order.ticks

    def _available(self, order: ClientOrder) -> int:
        opposite = OFFER if order.side == BUY else BID
        return sum(
            o.qty for o in self.md.books[order.symbol].values() if o.side == opposite and self._crosses(order, o.ticks)
        )

    def _match(self, order: ClientOrder) -> None:
        opposite = OFFER if order.side == BUY else BID
        while order.leaves:
            best = self.md.best_ticks(order.symbol, opposite)
            if best is None or not self._crosses(order, best):
                return
            resting = self.md.oldest_at(order.symbol, opposite, best)
            assert resting is not None
            qty = min(order.leaves, resting.qty)
            self.md.execute(order.symbol, resting, qty, order.side)
            self._fill(order, qty, best)

    def _on_resting_fill(self, symbol: str, resting: RestingOrder, qty: int, ticks: int) -> None:
        order = self.orders.get(resting.order_id)
        if order is not None:
            self._fill(order, qty, ticks)

    def _fill(self, order: ClientOrder, qty: int, ticks: int) -> None:
        order.cum_qty += qty
        order.notional_ticks += qty * ticks
        order.status = FILLED if order.cum_qty == order.qty else PARTIAL
        session = self.gateway.sessions.get(order.sender)
        if session is not None:
            self._report(session, order, EXEC_TRADE, last_qty=qty, last_ticks=ticks)

    # ---- reports ---------------------------------------------------------------------------

    def _report(
        self,
        session: SimSession,
        order: ClientOrder,
        exec_type: str,
        text: str = "",
        last_qty: int = 0,
        last_ticks: int | None = None,
        cl_ord_id: str | None = None,
        orig_cl_ord_id: str | None = None,
    ) -> None:
        sym = order.symbol
        inst = self.md.instruments.get(sym)
        tick = inst.tick if inst else 0.0
        avg = order.notional_ticks * tick / order.cum_qty if order.cum_qty else 0.0
        body: Fields = [
            (37, order.order_id if order.status != REJECTED else "NONE"),
            (11, cl_ord_id or order.cl_ord_id),
            (17, self.md.new_id("E")),
            (150, exec_type),
            (39, order.status),
            (1, order.account),
            (55, sym),
            (54, order.side),
            (38, str(order.qty)),
            (44, f"{order.ticks * tick:.2f}"),
            (59, order.tif),
            (14, str(order.cum_qty)),
            (151, str(order.leaves)),
            (6, f"{avg:.4f}"),
            (60, utc_timestamp()),
        ]
        if orig_cl_ord_id:
            body.append((41, orig_cl_ord_id))
        if last_ticks is not None:
            body += [(31, f"{last_ticks * tick:.2f}"), (32, str(last_qty))]
        if text:
            body.append((58, text))
        session.send("8", body)
