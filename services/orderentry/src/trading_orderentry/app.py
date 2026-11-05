"""The order entry session app: accepts orders from the api, checks them, sends them and tracks them."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Mapping
from decimal import Decimal

from trading_core import KillSwitchState, TraderBlock
from trading_fix import FixMessage, FixSession, SessionError, utc_timestamp

from .orders import FROM_ORD_STATUS, SIDE_TO_FIX, TIF_TO_FIX, Order, Status, now_iso
from .risk import Instrument, RiskLimits, Throttle, check_order
from .sink import OrderSink

log = logging.getLogger("oe")


class OrderError(Exception):
    """A request the caller got wrong (unknown order, someone else's order, already closed)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _price_text(price: float, tick: float) -> str:
    decimals = max(0, -Decimal(str(tick)).normalize().as_tuple().exponent)
    return f"{price:.{decimals}f}"


class OrderEntryApp:
    def __init__(
        self,
        sink: OrderSink,
        account: str,
        default_trader: str,
        limits: RiskLimits | None = None,
        throttle: Throttle | None = None,
        trader_ids: Mapping[str, str] | None = None,
        capacity: str = "2",
        instruments: Mapping[str, Instrument] | None = None,
    ):
        self.sink = sink
        self.account = account
        self.default_trader = default_trader
        self.trader_ids = dict(trader_ids or {})  # our user id -> ElectronX trader id for SenderSubID(50)
        self.capacity = capacity
        self.limits = limits or RiskLimits()
        self.throttle = throttle or Throttle(90)
        self.instruments: dict[str, Instrument] = dict(instruments or {})
        self.session: FixSession | None = None
        self.orders: dict[str, Order] = {}  # ClOrdID -> order
        self.positions: dict[tuple[str, str], int] = {}  # (trader, symbol) -> net lots, from our fills
        self._cancel_ids: dict[str, str] = {}  # cancel request ClOrdID -> order ClOrdID
        self._before_cancel: dict[str, Status] = {}
        self.kill_switch = KillSwitchState()
        self.blocks: dict[str, TraderBlock] = {}  # trader -> why they may not trade
        self._ids = itertools.count(1)
        self._id_prefix = f"T{int(time.time()):x}"

    # ---- requests from the api -------------------------------------------------------------

    def submit(self, trader: str, symbol: str, side: str, qty: int, price: float, tif: str = "DAY") -> Order:
        order = Order(self._new_id(), trader, symbol, side, qty, price, tif)
        self.orders[order.cl_ord_id] = order
        reason = self._precheck(order)
        if reason is None:
            try:
                self._send_new(order)
            except SessionError as exc:
                reason = str(exc)
        if reason is not None:
            order.status = Status.REJECTED
            order.text = reason
            log.info("rejected %s for %s: %s", order.cl_ord_id, trader, reason)
        else:
            log.info("sent %s %s %d %s @ %s for %s", order.cl_ord_id, side, qty, symbol, price, trader)
        self._publish(order)
        return order

    def cancel(self, trader: str, cl_ord_id: str) -> Order:
        order = self.orders.get(cl_ord_id)
        if order is None:
            raise OrderError(404, f"no order {cl_ord_id}")
        if order.trader != trader:
            raise OrderError(403, "not your order")
        if not order.live or order.status == Status.PENDING_CANCEL:
            raise OrderError(409, f"order is {order.status}")
        if not self._logged_on():
            raise OrderError(503, "not connected to the exchange")
        if not self.throttle.allow(is_cancel=True):
            raise OrderError(429, "message rate limit reached; try again in a moment")
        self._send_cancel(order)
        return order

    def set_kill_switch(self, state: KillSwitchState) -> None:
        """Called with the current kill switch state about once a second. While it is on, no new orders
        are accepted and every live order is cancelled (retried on each call until none are left)."""
        if state.active and not self.kill_switch.active:
            log.critical("kill switch ON (%s by %s): rejecting new orders, cancelling all", state.reason, state.by)
        elif not state.active and self.kill_switch.active:
            log.warning("kill switch OFF (by %s): accepting orders again", state.by)
        self.kill_switch = state
        if state.active and self._logged_on():
            for order in list(self.orders.values()):
                if order.live and order.status != Status.PENDING_CANCEL:
                    if not self.throttle.allow(is_cancel=True):
                        return  # the rest go out on the next call
                    self._send_cancel(order)

    def set_blocks(self, blocks: Mapping[str, TraderBlock]) -> None:
        """Called with the current trader blocks about once a second. A blocked trader's new orders are
        rejected and their live orders cancelled, the same way the kill switch does for everyone."""
        for trader in blocks.keys() - self.blocks.keys():
            log.critical(
                "trader %s BLOCKED (%s by %s): cancelling their orders",
                trader,
                blocks[trader].reason,
                blocks[trader].by,
            )
        for trader in self.blocks.keys() - blocks.keys():
            log.warning("trader %s unblocked", trader)
        self.blocks = dict(blocks)
        if blocks and self._logged_on():
            for order in list(self.orders.values()):
                if order.trader in blocks and order.live and order.status != Status.PENDING_CANCEL:
                    if not self.throttle.allow(is_cancel=True):
                        return  # the rest go out on the next call
                    self._send_cancel(order)

    def positions_by_trader(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for (trader, symbol), qty in self.positions.items():
            out.setdefault(trader, {})[symbol] = qty
        return out

    def _send_cancel(self, order: Order) -> None:
        cancel_id = self._new_id()
        self._cancel_ids[cancel_id] = order.cl_ord_id
        self._before_cancel[order.cl_ord_id] = order.status
        assert self.session is not None
        self.session.send(
            "F",
            [
                (11, cancel_id),
                (41, order.cl_ord_id),
                (1, self.account),
                (55, order.symbol),
                (54, SIDE_TO_FIX[order.side]),
                (38, str(order.qty)),
                (60, utc_timestamp()),
            ],
            header=[(50, self._exchange_trader(order.trader))],
        )
        self._update(order, status=Status.PENDING_CANCEL)

    def orders_for(self, trader: str | None = None) -> list[Order]:
        """All orders (trader=None) or one trader's, newest first."""
        found = [o for o in self.orders.values() if trader is None or o.trader == trader]
        return sorted(found, key=lambda o: o.created, reverse=True)

    # ---- SessionApp ------------------------------------------------------------------------

    def on_logon(self, session: FixSession) -> None:
        self.session = session
        log.info("order entry session logged on")

    def on_disconnect(self, session: FixSession) -> None:
        log.warning("order entry session down: new orders are rejected until it is back")

    def on_app_message(self, session: FixSession, msg: FixMessage) -> None:
        mt = msg.msg_type
        if mt == "8":
            self._on_execution_report(msg)
        elif mt == "9":
            self._on_cancel_reject(msg)
        elif mt == "j":
            order = self.orders.get(msg.get(379, ""))
            log.error("business reject of %s: %s", msg.get(379), msg.get(58))
            if order is not None and order.status == Status.PENDING_NEW:
                self._update(order, status=Status.REJECTED, text=msg.get(58, ""))

    # ---- exchange messages -----------------------------------------------------------------

    def _on_execution_report(self, msg: FixMessage) -> None:
        cl = msg.get(11, "")
        order = self.orders.get(self._cancel_ids.get(cl, cl))
        if order is None:
            log.warning("ExecutionReport for unknown order %s (from an earlier run?)", cl)
            return
        exec_type = msg.get(150, "")
        if exec_type == "F":
            last_qty = msg.get_int(32)
            sign = 1 if order.side == "BUY" else -1
            key = (order.trader, order.symbol)
            self.positions[key] = self.positions.get(key, 0) + sign * last_qty
            log.info("fill %s %d @ %s", cl, last_qty, msg.get(31))
        order_id = msg.get(37, "")
        self._update(
            order,
            status=FROM_ORD_STATUS.get(msg.get(39, ""), order.status),
            order_id=order_id if order_id and order_id != "NONE" else order.order_id,
            cum_qty=msg.get_int(14, order.cum_qty),
            avg_px=msg.get_float(6, order.avg_px),
            text=msg.get(58, order.text if exec_type != "F" else ""),
        )

    def _on_cancel_reject(self, msg: FixMessage) -> None:
        order = self.orders.get(msg.get(41, "")) or self.orders.get(self._cancel_ids.get(msg.get(11, ""), ""))
        if order is None:
            return
        log.info("cancel of %s rejected: %s", order.cl_ord_id, msg.get(58))
        previous = self._before_cancel.pop(order.cl_ord_id, order.status)
        status = FROM_ORD_STATUS.get(msg.get(39, ""), previous)
        self._update(order, status=status if status != Status.REJECTED else previous, text=msg.get(58, ""))

    # ---- helpers ---------------------------------------------------------------------------

    def _precheck(self, order: Order) -> str | None:
        if self.kill_switch.active:
            return f"trading is halted (kill switch: {self.kill_switch.reason})"
        block = self.blocks.get(order.trader)
        if block is not None:
            return f"you are blocked from trading: {block.reason}"
        if order.side not in SIDE_TO_FIX:
            return f"side must be BUY or SELL, not {order.side}"
        if order.tif not in TIF_TO_FIX:
            return f"time in force must be one of {', '.join(TIF_TO_FIX)}"
        open_orders = [o for o in self.orders.values() if o.trader == order.trader and o.live and o is not order]
        position = self.positions.get((order.trader, order.symbol), 0)
        reason = check_order(order, self.instruments.get(order.symbol), self.limits, open_orders, position)
        if reason is not None:
            return reason
        if not self._logged_on():
            return "not connected to the exchange"
        if not self.throttle.allow():
            return "message rate limit reached; try again in a moment"
        return None

    def _send_new(self, order: Order) -> None:
        assert self.session is not None
        inst = self.instruments[order.symbol]
        self.session.send(
            "D",
            [
                (11, order.cl_ord_id),
                (1, self.account),
                (55, order.symbol),
                (54, SIDE_TO_FIX[order.side]),
                (38, str(order.qty)),
                (40, "2"),  # limit
                (44, _price_text(order.price, inst.tick_size)),
                (59, TIF_TO_FIX[order.tif]),
                (60, utc_timestamp()),
                (582, self.capacity),
                (1028, "Y"),  # ManualOrderIndicator: entered by a person
            ],
            header=[(50, self._exchange_trader(order.trader))],
        )

    def _exchange_trader(self, trader: str) -> str:
        return self.trader_ids.get(trader, self.default_trader)

    def _logged_on(self) -> bool:
        return self.session is not None and self.session.logged_on

    def _new_id(self) -> str:
        return f"{self._id_prefix}-{next(self._ids)}"

    def _update(self, order: Order, **changes) -> None:
        for key, value in changes.items():
            setattr(order, key, value)
        order.updated = now_iso()
        if not order.live:
            self._before_cancel.pop(order.cl_ord_id, None)
        self._publish(order)

    def _publish(self, order: Order) -> None:
        self.sink.publish(order.to_dict())
