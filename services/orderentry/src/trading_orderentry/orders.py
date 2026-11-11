"""Our record of each order, kept up to date from the exchange's ExecutionReports.

Every change is published as the order's new state plus the event that caused it (OrderEntryApp):
  event               new_order, refused (by our checks, never sent), cancel_request, cancel_refused (by
                      us: not your order, already closed, ...), exchange_report, trade_bust (the
                      exchange cancelled one of its fills), cancel_reject, business_reject,
                      session_reject (the exchange's session refused the message as malformed)
  by                  who or what caused it: the trader's user id, the risk user who tripped the kill switch
                      or blocked the trader (or riskmonitor, dropcopy), orderentry itself, or "exchange"
  reason, event_time  why (a reject or refusal, the kill switch's reason, ...), and when it happened here
  request_id          the ClOrdID of the cancel request an event belongs to
and, for exchange reports, the fields of trading_fix.report_fields (ExecID, ExecType, the fill, ...).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PENDING_NEW = "pending_new"  # sent, no answer from the exchange yet
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELED = "canceled"
    REJECTED = "rejected"


LIVE = {Status.PENDING_NEW, Status.NEW, Status.PARTIALLY_FILLED, Status.PENDING_CANCEL}

#: OrdStatus(39) -> Status
FROM_ORD_STATUS = {
    "0": Status.NEW,
    "1": Status.PARTIALLY_FILLED,
    "2": Status.FILLED,
    "4": Status.CANCELED,
    "6": Status.PENDING_CANCEL,
    "8": Status.REJECTED,
    "A": Status.PENDING_NEW,
}

SIDE_TO_FIX = {"BUY": "1", "SELL": "2"}
SIDE_FROM_FIX = {v: k for k, v in SIDE_TO_FIX.items()}
TIF_TO_FIX = {"DAY": "0", "GTC": "1", "IOC": "3", "FOK": "4"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class Order:
    cl_ord_id: str
    trader: str  # our user id (e.g. alice), not the exchange trader id
    symbol: str
    side: str  # BUY or SELL
    qty: int
    price: float
    tif: str  # DAY, GTC, IOC or FOK
    status: Status = Status.PENDING_NEW
    order_id: str = ""  # the exchange's OrderID(37)
    cum_qty: int = 0
    avg_px: float = 0.0
    text: str = ""
    created: str = field(default_factory=now_iso)
    updated: str = field(default_factory=now_iso)
    version: int = 0  # +1 on every published update, so an older copy never overwrites a newer one
    account: str = ""  # Account(1) it was sent with
    exchange_user: str = ""  # SenderSubID(50) it was sent with: the trader's ElectronX id
    capacity: str = ""  # CustOrderCapacity(582)
    ord_type: str = "LIMIT"  # OrdType(40): limit orders only
    manual: bool = True  # ManualOrderIndicator(1028): entered by a person
    busted_qty: int = 0  # lots of its fills the exchange has since cancelled (trade busts)
    busted: list[str] = field(default_factory=list)  # the ExecIDs of those fills

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Order:
        """The inverse of to_dict (extra keys such as `leaves` are ignored)."""
        names = {f.name for f in fields(cls)}
        order = cls(**{k: v for k, v in data.items() if k in names})
        order.status = Status(order.status)
        return order

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def net_filled(self) -> int:
        """What counts towards the position: filled, less what the exchange has busted."""
        return self.cum_qty - self.busted_qty

    @property
    def leaves(self) -> int:
        return self.qty - self.cum_qty if self.live else 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = str(self.status)
        d["leaves"] = self.leaves
        return d
