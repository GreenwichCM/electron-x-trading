"""The order entry session app: accepts orders from the api, checks them, sends them and tracks them."""

from __future__ import annotations

import itertools
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from decimal import Decimal

from trading_core import KillSwitchState, TraderBlock, TradingRules
from trading_fix import FixMessage, FixSession, SessionError, report_fields, utc_timestamp

from .orders import FROM_ORD_STATUS, LIVE, SIDE_TO_FIX, TIF_TO_FIX, Order, Status, now_iso
from .recovery import Recovered
from .risk import Instrument, RiskLimits, Throttle, check_order
from .sink import OrderSink

log = logging.getLogger("oe")

SENT_KEPT = 10_000  # orders and cancels remembered by sequence number, for session-level rejects


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
        rules: TradingRules | None = None,
        throttle: Throttle | None = None,
        capacity: str = "2",
        instruments: Mapping[str, Instrument] | None = None,
    ):
        self.sink = sink
        self.account = account
        self.default_trader = default_trader  # ElectronX trader id for anyone without their own
        self.capacity = capacity
        self.rules = rules  # from SQL Server (set_rules); until then every new order is rejected
        self.throttle = throttle or Throttle(90)
        self.instruments: dict[str, Instrument] = dict(instruments or {})
        self.session: FixSession | None = None
        self.orders: dict[str, Order] = {}  # ClOrdID -> order
        self.positions: dict[tuple[str, str], int] = {}  # (trader, symbol) -> net lots, from our fills
        self.screen_by_user: dict[str, dict[str, int]] = {}  # ElectronX user -> symbol -> lots from their screen
        self._cancel_ids: dict[str, str] = {}  # cancel request ClOrdID -> order ClOrdID
        self._before_cancel: dict[str, Status] = {}
        self._sent: OrderedDict[int, tuple[str, str]] = OrderedDict()  # MsgSeqNum -> (ClOrdID, cancel's or "")
        self.kill_switch = KillSwitchState()
        self.blocks: dict[str, TraderBlock] = {}  # trader -> why they may not trade
        self.changes = 0  # goes up on every order update; the state file is saved when it moves
        self._unconfirmed: set[str] = set()  # restored from an earlier FIX week, still to be cancelled
        self._ids = itertools.count(1)
        self._id_prefix = f"T{time.time_ns() // 1_000_000:x}"  # milliseconds: unique even after a quick restart

    # ---- restart -----------------------------------------------------------------------------

    def restore(self, rec: Recovered, from_earlier_week: bool = False) -> None:
        """Take over the orders and positions of the previous run (call before the session starts)."""
        self.orders = dict(rec.orders)
        self.positions = dict(rec.positions)
        live = [o for o in self.orders.values() if o.live]
        log.info(
            "restored %d orders (%d live) and %d positions; %d updates replayed from the orders stream",
            len(self.orders),
            len(live),
            len(self.positions),
            rec.replayed,
        )
        # A cancel sent just before the restart may never have left: send those again.
        self._unconfirmed = {o.cl_ord_id for o in live if o.status == Status.PENDING_CANCEL}
        if from_earlier_week and live:
            self._unconfirmed = {o.cl_ord_id for o in live}
            log.warning(
                "%d live orders are from before the weekly reset and cannot be confirmed: cancelling them",
                len(live),
            )

    def _restart_reason(self, order: Order) -> str:
        if order.status == Status.PENDING_CANCEL:
            return "after a restart: the cancel sent before it is not confirmed"
        return "after a restart: from before the weekly sequence reset, its state cannot be confirmed"

    def cancel_unconfirmed(self) -> None:
        """Called about once a second: send one cancel for each restored order that needs it (from an
        earlier FIX week, or with a cancel in flight at the restart). The exchange either cancels it or
        answers that it does not know it; both close it here."""
        if not self._unconfirmed or not self._logged_on():
            return
        for cl in list(self._unconfirmed):
            order = self.orders.get(cl)
            if order is None or not order.live:
                self._unconfirmed.discard(cl)
                continue
            if not self.throttle.allow(is_cancel=True):
                return  # the rest go out on the next call
            self._unconfirmed.discard(cl)
            self._send_cancel(order, by="orderentry", reason=self._restart_reason(order))

    # ---- requests from the api -------------------------------------------------------------

    def submit(self, trader: str, symbol: str, side: str, qty: int, price: float, tif: str = "DAY") -> Order:
        order = Order(self._new_id(), trader, symbol, side, qty, price, tif)
        order.account, order.capacity = self.account, self.capacity
        order.exchange_user = self._exchange_trader(trader)
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
        self._publish(order, {"event": "refused" if reason else "new_order", "by": trader, "reason": reason or ""})
        return order

    def cancel(self, trader: str, cl_ord_id: str) -> Order:
        order = self.orders.get(cl_ord_id)
        if order is None:
            raise OrderError(404, f"no order {cl_ord_id}")

        def refuse(status_code: int, why: str) -> OrderError:
            """A cancel that never left: recorded against the order (who asked, and why not), then refused."""
            self._publish(order, {"event": "cancel_refused", "by": trader, "reason": why})
            return OrderError(status_code, why)

        if order.trader != trader:
            raise refuse(403, "not your order")
        if not order.live or order.status == Status.PENDING_CANCEL:
            raise refuse(409, f"order is {order.status}")
        if not self._logged_on():
            raise refuse(503, "not connected to the exchange")
        if not self.throttle.allow(is_cancel=True):
            raise refuse(429, "message rate limit reached; try again in a moment")
        self._send_cancel(order, by=trader)
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
                    self._send_cancel(order, by=state.by or "killswitch", reason=f"kill switch: {state.reason}")

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
                    block = blocks[order.trader]
                    self._send_cancel(order, by=block.by or "block", reason=f"trader blocked: {block.reason}")

    def set_rules(self, rules: TradingRules) -> None:
        """New trading rules (entitlements and limits) for orders from now on. Live orders are kept."""
        self.rules = rules

    def set_screen_positions(self, by_user: Mapping[str, Mapping[str, int]]) -> None:
        """What each ElectronX user holds from orders entered on the ElectronX screen (from dropcopy)."""
        self.screen_by_user = {user: dict(held) for user, held in by_user.items()}

    def screen_position(self, trader: str, symbol: str) -> int:
        """The trader's position in `symbol` from the ElectronX screen. It counts against the same
        position limit as orders sent from here."""
        user = self._exchange_trader(trader)
        if self.rules is None or self.rules.trader_for(user) != trader:
            return 0  # this trader's ElectronX id is shared or unknown: it cannot say whose it is
        return self.screen_by_user.get(user, {}).get(symbol, 0)

    def positions_by_trader(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for (trader, symbol), qty in self.positions.items():
            out.setdefault(trader, {})[symbol] = qty
        return out

    def _send_cancel(self, order: Order, by: str, reason: str = "") -> None:
        """Send a cancel; `by` and `reason` say who or what asked for it (a trader, the kill switch, ...)."""
        cancel_id = self._new_id()
        self._cancel_ids[cancel_id] = order.cl_ord_id
        self._before_cancel[order.cl_ord_id] = order.status
        assert self.session is not None
        seq = self.session.send(
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
            header=[(50, order.exchange_user or self._exchange_trader(order.trader))],
        )
        self._remember(seq, order.cl_ord_id, cancel_id)
        event = {"event": "cancel_request", "by": by, "reason": reason, "request_id": cancel_id}
        self._update(order, event, status=Status.PENDING_CANCEL)

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
        elif mt == "3":
            self._on_session_reject(msg)
        elif mt == "j":
            order = self.orders.get(msg.get(379, "")) or self.orders.get(self._cancel_ids.get(msg.get(379, ""), ""))
            log.error("business reject of %s: %s", msg.get(379), msg.get(58))
            if order is not None:
                event = {
                    "event": "business_reject",
                    "by": "exchange",
                    "reason": msg.get(58, ""),
                    "reject_code": msg.get_int(380, 0) if msg.get(380) else None,
                    "request_id": msg.get(379, ""),
                }
                if order.status == Status.PENDING_NEW:
                    self._update(order, event, status=Status.REJECTED, text=msg.get(58, ""))
                else:
                    self._publish(order, event)  # for example a throttled cancel: the order itself is unchanged

    # ---- exchange messages -----------------------------------------------------------------

    def _on_execution_report(self, msg: FixMessage) -> None:
        cl = msg.get(11, "")
        exec_type = msg.get(150, "")
        # A cancel's report carries the cancel's own ClOrdID in 11 and the order's in OrigClOrdID(41);
        # after a restart only the second is known.
        order = self.orders.get(self._cancel_ids.get(cl, cl)) or self.orders.get(msg.get(41, ""))
        if order is None:
            if exec_type in ("F", "H"):
                log.error(
                    "%s for unknown order %s: not in our positions (dropcopy will show the difference)",
                    "fill" if exec_type == "F" else "trade bust",
                    cl,
                )
            else:
                log.warning("ExecutionReport for unknown order %s", cl)
            return
        if exec_type == "H":
            self._on_bust(order, msg)
            return
        cum_qty = msg.get_int(14, order.cum_qty)
        status = FROM_ORD_STATUS.get(msg.get(39, ""), order.status)
        if cum_qty < order.cum_qty or (not order.live and status in LIVE):
            log.info("ignoring an older ExecutionReport for %s (%s, %d filled)", order.cl_ord_id, status, cum_qty)
            return
        if cum_qty > order.cum_qty:  # by the change in filled qty, so a report seen twice counts once
            filled = cum_qty - order.cum_qty
            key = (order.trader, order.symbol)
            self.positions[key] = self.positions.get(key, 0) + (filled if order.side == "BUY" else -filled)
        if exec_type == "F":
            log.info("fill %s %d @ %s", cl, msg.get_int(32), msg.get(31))
        order_id = msg.get(37, "")
        event = {"event": "exchange_report", "by": "exchange", "reason": msg.get(58, ""), **report_fields(msg)}
        if cl != order.cl_ord_id:
            event["request_id"] = cl  # the report of a cancel carries the cancel request's ClOrdID
        self._update(
            order,
            event,
            status=status,
            order_id=order_id if order_id and order_id != "NONE" else order.order_id,
            cum_qty=cum_qty,
            avg_px=msg.get_float(6, order.avg_px),
            text=msg.get(58, order.text if exec_type != "F" else ""),
        )

    def _on_bust(self, order: Order, msg: FixMessage) -> None:
        """The exchange cancelled (busted) one of this order's trades: that fill no longer counts. The
        report repeats the order as it was at the trade, so only the busted quantity changes."""
        ref = msg.get(19, "") or msg.get(17, "")  # ExecRefID: the fill it cancels
        if ref in order.busted:
            return  # a resent copy
        qty = msg.get_int(32)
        order.busted.append(ref)
        order.busted_qty += qty
        key = (order.trader, order.symbol)
        self.positions[key] = self.positions.get(key, 0) - (qty if order.side == "BUY" else -qty)
        log.warning(
            "trade bust: fill %s of %s (%s %d %s) cancelled by the exchange: %s",
            ref,
            order.cl_ord_id,
            order.side,
            qty,
            order.symbol,
            msg.get(58, "") or "no reason given",
        )
        event = {"event": "trade_bust", "by": "exchange", "reason": msg.get(58, ""), **report_fields(msg)}
        self._update(order, event)

    def _on_session_reject(self, msg: FixMessage) -> None:
        """The exchange's session refused one of our messages (Reject [3]), so it never reached the book.
        The reject names only the message's sequence number (RefSeqNum 45)."""
        try:
            cl, request_id = self._sent.pop(int(msg.get(45) or ""))
        except KeyError, ValueError:
            return  # not an order or cancel of ours (the session has logged it)
        order = self.orders.get(cl)
        if order is None:
            return
        reason = f"refused by the exchange's session: {msg.get(58) or 'no reason given'}"
        if msg.get(371):
            reason += f" (tag {msg.get(371)})"  # RefTagID: the field it objected to
        event = {
            "event": "session_reject",
            "by": "exchange",
            "reason": reason,
            "reject_code": msg.get_int(373) if msg.get(373) else None,  # SessionRejectReason
            "request_id": request_id,
        }
        if not request_id and order.status == Status.PENDING_NEW:
            self._update(order, event, status=Status.REJECTED, text=reason)
        elif request_id and order.status == Status.PENDING_CANCEL:
            previous = self._before_cancel.pop(order.cl_ord_id, Status.NEW)
            self._update(order, event, status=previous, text=reason)
        else:
            self._publish(order, event)  # already answered otherwise: recorded, nothing changes

    def _on_cancel_reject(self, msg: FixMessage) -> None:
        order = self.orders.get(msg.get(41, "")) or self.orders.get(self._cancel_ids.get(msg.get(11, ""), ""))
        if order is None:
            return
        log.info("cancel of %s rejected: %s", order.cl_ord_id, msg.get(58))
        event = {
            "event": "cancel_reject",
            "by": "exchange",
            "reason": msg.get(58, ""),
            "reject_code": msg.get_int(102, 0) if msg.get(102) else None,  # CxlRejReason
            "request_id": msg.get(11, ""),
            "exchange_time": msg.get(60, ""),
        }
        if msg.get(102) == "1":  # CxlRejReason 1 = unknown order: it is not live at the exchange
            self._before_cancel.pop(order.cl_ord_id, None)
            text = f"not at the exchange ({msg.get(58, 'unknown order')})"
            self._update(order, event, status=Status.CANCELED, text=text)
            return
        previous = self._before_cancel.pop(order.cl_ord_id, order.status)
        status = FROM_ORD_STATUS.get(msg.get(39, ""), previous)
        self._update(order, event, status=status if status != Status.REJECTED else previous, text=msg.get(58, ""))

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
        limits, reason = self._limits_for(order)
        if limits is None:
            return reason
        open_orders = [o for o in self.orders.values() if o.trader == order.trader and o.live and o is not order]
        key = (order.trader, order.symbol)
        position = self.positions.get(key, 0) + self.screen_position(*key)  # the screen's counts too
        reason = check_order(order, self.instruments.get(order.symbol), limits, open_orders, position)
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
        seq = self.session.send(
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
            header=[(50, order.exchange_user)],
        )
        self._remember(seq, order.cl_ord_id)

    def _limits_for(self, order: Order) -> tuple[RiskLimits | None, str]:
        """The limits for this order from the trading rules, or None and why it may not be placed."""
        if self.rules is None:
            return None, "trading rules are not loaded yet (is SQL Server reachable?)"
        reason = self.rules.check_trader(order.trader, order.symbol)
        if reason is not None:
            return None, reason
        lim = self.rules.limits_for(order.trader, order.symbol)
        missing = lim.missing("max_order_qty", "max_order_notional", "max_open_orders", "max_position")
        if missing:
            return None, f"no {', '.join(missing)} limit is set for {order.trader} on {order.symbol}"
        return RiskLimits(lim.max_order_qty, lim.max_order_notional, lim.max_open_orders, lim.max_position), ""

    def _exchange_trader(self, trader: str) -> str:
        return (self.rules.exchange_trader(trader) if self.rules else None) or self.default_trader

    def _remember(self, seq: int, cl_ord_id: str, request_id: str = "") -> None:
        """Which order a message was about, by its sequence number, in case the session rejects it."""
        self._sent[seq] = (cl_ord_id, request_id)
        while len(self._sent) > SENT_KEPT:
            self._sent.popitem(last=False)

    def _logged_on(self) -> bool:
        return self.session is not None and self.session.logged_on

    def _new_id(self) -> str:
        return f"{self._id_prefix}-{next(self._ids)}"

    def _update(self, order: Order, event: dict, **changes) -> None:
        for key, value in changes.items():
            setattr(order, key, value)
        order.updated = now_iso()
        if not order.live:
            self._before_cancel.pop(order.cl_ord_id, None)
        self._publish(order, event)

    def _publish(self, order: Order, event: dict) -> None:
        """The order's new state, with the event that caused it (see orders.py)."""
        order.version += 1
        self.changes += 1
        self.sink.publish({**order.to_dict(), "event_time": now_iso(), **event})
