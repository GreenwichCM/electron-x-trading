"""The drop copy session app: records every fill the exchange reports for the account."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from trading_fix import FixMessage, FixSession

from .journal import FillJournal

log = logging.getLogger("dc")

SIDES = {"1": "BUY", "2": "SELL"}


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


class DropCopyApp:
    """Positions per symbol for the whole account, built only from the exchange's drop copy."""

    def __init__(self, sink: FillSink, journal: FillJournal | None = None):
        self.sink = sink
        self.journal = journal
        self.positions: dict[str, int] = {}
        self.seen: set[str] = set()  # ExecIDs already counted
        self.connected = False
        if journal is not None:
            replayed = journal.load()
            for fill in replayed:
                self._apply(fill)
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
        if msg.msg_type != "8" or msg.get(150) != "F":  # only trades change positions
            return
        exec_id = msg.get(17, "")
        if not exec_id or exec_id in self.seen:
            return  # a resent copy of a fill we already have
        fill = {
            "exec_id": exec_id,
            "order_id": msg.get(37, ""),
            "cl_ord_id": msg.get(11, ""),
            "account": msg.get(1, ""),
            "symbol": msg.get(55, ""),
            "side": SIDES.get(msg.get(54, ""), ""),
            "qty": msg.get_int(32),
            "price": msg.get_float(31),
            "time": msg.get(60, ""),
        }
        if self.journal is not None:
            self.journal.append(fill)
        self._apply(fill)
        log.info("fill %s %s %d %s @ %s", fill["exec_id"], fill["side"], fill["qty"], fill["symbol"], fill["price"])
        self.sink.fill(fill)
        self.sink.positions(self.positions, self.connected)

    def _apply(self, fill: dict[str, Any]) -> None:
        self.seen.add(fill["exec_id"])
        sign = 1 if fill["side"] == "BUY" else -1
        self.positions[fill["symbol"]] = self.positions.get(fill["symbol"], 0) + sign * int(fill["qty"])
