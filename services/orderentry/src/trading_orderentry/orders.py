"""Our record of each order, kept up to date from the exchange's ExecutionReports."""

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
    def leaves(self) -> int:
        return self.qty - self.cum_qty if self.live else 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = str(self.status)
        d["leaves"] = self.leaves
        return d
