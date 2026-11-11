"""Order entry side of the simulated gateway: limit orders, cancels, matching and ExecutionReports.

Follows the ElectronX rules the OMS depends on:
  * NewOrderSingle [D] and OrderCancelRequest [F] need SenderSubID(50) (the trader) in the header
  * limit orders only (OrdType 40=2), TimeInForce DAY(0), GTC(1), IOC(3) or FOK(4)
  * price on the tick and inside the instrument's limits, instrument must be OPEN
  * price-time matching against everything resting in the shared book
  * rejects come back as ExecutionReport 150=8, unknown cancels as OrderCancelReject [9]
  * every ExecutionReport is also copied to the drop copy sessions, which share this port and may
    not send orders themselves; it names the trader in TargetSubID(57)
  * screen_order() places an order as a trader would on the ElectronX screen: no FIX session owns it,
    so only the drop copy sessions hear about it
  * resting orders survive a restart of the simulator (with a `state_path`), as they would at the
    exchange, until the weekly reset
  * bust() cancels a trade after the fact, as the exchange's operations team can: an ExecutionReport
    with ExecType H for each side of ours, repeating the order as it was at the trade, with
    ExecRefID(19) naming the fill it cancels
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from trading_fix import FixMessage, fix_week_start, utc_timestamp

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
    manual: bool = False  # ManualOrderIndicator(1028): entered by a person
    capacity: str = "2"  # CustOrderCapacity(582)

    @property
    def leaves(self) -> int:
        return self.qty - self.cum_qty if self.status in (NEW, PARTIAL) else 0


class SimOrderEntry:
    """Attach to the order entry SimGateway; shares the book of `md`."""

    def __init__(
        self,
        gateway: SimGateway,
        md: SimMarketData,
        drop_copy_senders: Iterable[str] = (),
        state_path: Path | None = None,
    ):
        self.gateway = gateway
        self.md = md
        self.drop_copy_senders = set(drop_copy_senders)
        self.state_path = state_path
        gateway.on_app_message = self._on_message
        md.on_fill = self._on_resting_fill
        md.on_expire = self._on_expire
        self.orders: dict[str, ClientOrder] = {}  # OrderID -> order
        self.by_cl_ord_id: dict[tuple[str, str], ClientOrder] = {}  # (SenderCompID, ClOrdID) -> order
        # TrdMatchID -> the trade reports sent for it (sender, fields), for bust()
        self.trade_reports: dict[str, list[tuple[str, Fields]]] = {}
        self._load()

    # ---- requests --------------------------------------------------------------------------

    def _on_message(self, session: SimSession, msg: FixMessage) -> None:
        if session.sender in self.drop_copy_senders:
            business_reject(session, msg, 3, "Drop copy sessions cannot send orders")
        elif msg.msg_type in ("D", "F") and not msg.get(50):
            business_reject(session, msg, 5, "SenderSubID(50) required on order messages")
        elif msg.msg_type == "D":
            self._new_order(session, msg)
        elif msg.msg_type == "F":
            self._cancel(session, msg)
        else:
            business_reject(session, msg, 3, "Unsupported Message Type")
        self.md.flush()
        self._save()

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
            manual=msg.get(1028) == "Y",
            capacity=msg.get(582) or "2",
        )
        self._enter(session, order, price, msg.get(40, ""))

    def screen_order(self, user: str, symbol: str, side: str, qty: int, price: float, tif: str = "0") -> ClientOrder:
        """An order a trader enters on the ElectronX screen. `side` is BUY ("1") or SELL ("2")."""
        inst = self.md.instruments.get(symbol)
        order = ClientOrder(
            order_id=self.md.new_id("O"),
            cl_ord_id=self.md.new_id("UI"),
            sender="",
            trader=user,
            account="",
            symbol=symbol,
            side=side,
            qty=qty,
            ticks=round(price / inst.tick) if inst else 0,
            tif=tif,
            manual=True,
        )
        self._enter(None, order, price, "2")
        self.md.flush()
        self._save()
        return order

    def screen_cancel(self, order_id: str) -> None:
        """The trader cancels an order of theirs on the ElectronX screen."""
        order = self.orders[order_id]
        if not order.leaves:
            return
        if order.order_id in self.md.books[order.symbol]:
            self.md.cancel_order(order.symbol, order.order_id)
        order.status = CANCELED
        self._report(None, order, EXEC_CANCELED)
        self.md.flush()
        self._save()

    def bust(self, trade_id: str = "", reason: str = "Trade busted by ElectronX Operations") -> int:
        """Cancel a trade (the latest one if no TrdMatchID is given). Returns the number of reports sent:
        one per side of the trade that was ours or from the screen, none if there is no such trade."""
        trade_id = trade_id or next(reversed(self.trade_reports), "")
        sent = self.trade_reports.pop(trade_id, [])
        for sender, body in sent:
            fields = dict(body)
            out: Fields = []
            for tag, value in body:
                if tag == 17:
                    value = self.md.new_id("E")
                elif tag == 150:
                    value = "H"
                elif tag == 60:
                    value = utc_timestamp()  # when the bust happened; the rest is as at the trade
                out.append((tag, value))
            out += [(19, fields[17]), (58, reason)]
            self._send_report(self.gateway.sessions.get(sender) if sender else None, out)
        return len(sent)

    def _enter(self, session: SimSession | None, order: ClientOrder, price: float, ord_type: str) -> None:
        """Check a new order, then acknowledge it, match it and rest what is left."""
        inst = self.md.instruments.get(order.symbol)
        cl, symbol, side, qty, ticks, tif = order.cl_ord_id, order.symbol, order.side, order.qty, order.ticks, order.tif
        reason = ""
        if (order.sender, cl) in self.by_cl_ord_id or not cl:
            reason = "Duplicate or missing ClOrdID"
        elif inst is None:
            reason = f"Unknown security {symbol}"
        elif inst.state != "OPEN":
            reason = f"Book is {inst.state}"
        elif ord_type != "2":
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
        self.by_cl_ord_id[(order.sender, cl)] = order
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
            self.md.add_order(symbol, book_side, ticks, order.leaves, owner=order.sender, order_id=order.order_id)

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
            self._fill(order, qty, best, aggressor=True)

    def _on_resting_fill(self, symbol: str, resting: RestingOrder, qty: int, ticks: int) -> None:
        order = self.orders.get(resting.order_id)
        if order is not None:
            self._fill(order, qty, ticks, aggressor=False)
            self._save()

    def _on_expire(self, symbol: str, resting: RestingOrder) -> None:
        order = self.orders.get(resting.order_id)
        if order is None or not order.leaves:
            return
        order.status = CANCELED
        self._report(self.gateway.sessions.get(order.sender), order, EXEC_CANCELED, text="Instrument expired")
        self._save()

    def _fill(self, order: ClientOrder, qty: int, ticks: int, aggressor: bool) -> None:
        order.cum_qty += qty
        order.notional_ticks += qty * ticks
        order.status = FILLED if order.cum_qty == order.qty else PARTIAL
        session = self.gateway.sessions.get(order.sender)  # None for an order from the screen
        self._report(session, order, EXEC_TRADE, last_qty=qty, last_ticks=ticks, aggressor=aggressor)

    # ---- reports ---------------------------------------------------------------------------

    def _report(
        self,
        session: SimSession | None,
        order: ClientOrder,
        exec_type: str,
        text: str = "",
        last_qty: int = 0,
        last_ticks: int | None = None,
        cl_ord_id: str | None = None,
        orig_cl_ord_id: str | None = None,
        aggressor: bool | None = None,
    ) -> None:
        sym = order.symbol
        inst = self.md.instruments.get(sym)
        tick = inst.tick if inst else 0.0
        avg = order.notional_ticks * tick / order.cum_qty if order.cum_qty else 0.0
        body: Fields = [
            (57, order.trader),  # TargetSubID: a header field; the gateway writes it straight after 56
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
            (1028, "Y" if order.manual else "N"),
            (581, "1"),  # AccountType: customer, as on the connectivity sheet
            (582, order.capacity),  # CustOrderCapacity, echoed
        ]
        if orig_cl_ord_id:
            body.append((41, orig_cl_ord_id))
        if last_ticks is not None:
            body += [(31, f"{last_ticks * tick:.2f}"), (32, str(last_qty))]
            body += [(880, self.md.last_trade_id), (1057, "Y" if aggressor else "N")]  # TrdMatchID, AggressorIndicator
        if text:
            body.append((58, text))
        if exec_type == EXEC_TRADE:
            self.trade_reports.setdefault(self.md.last_trade_id, []).append((order.sender, list(body)))
        self._send_report(session, body)

    def _send_report(self, session: SimSession | None, body: Fields) -> None:
        if session is not None:
            session.send("8", body)
        for sender in self.drop_copy_senders:
            # Kept for resend even while the drop copy session is disconnected, like on the real gateway.
            self.gateway.session(sender).send("8", list(body))

    # ---- resting orders across simulator restarts ------------------------------------------

    def _save(self) -> None:
        if self.state_path is None:
            return
        resting = [asdict(o) for o in self.orders.values() if o.leaves]
        data = {"week": fix_week_start().isoformat(), "orders": resting}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("week") != fix_week_start().isoformat():
            log.info("weekly reset since the simulator last ran: client orders from last week are gone")
            return
        for fields in data.get("orders", []):
            order = ClientOrder(**fields)
            if order.symbol not in self.md.books:
                continue
            self.orders[order.order_id] = order
            self.by_cl_ord_id[(order.sender, order.cl_ord_id)] = order
            side = BID if order.side == BUY else OFFER
            self.md.add_order(
                order.symbol, side, order.ticks, order.leaves, owner=order.sender, order_id=order.order_id
            )
        self.md.flush()
        log.info("restored %d resting client orders", len(self.orders))
