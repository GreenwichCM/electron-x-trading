"""The market data session app: reference data, subscriptions, one OrderBook per symbol, publishing."""

from __future__ import annotations

import itertools
import logging
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from trading_fix import FixMessage, FixSession

from .book import BID, NEW, OFFER, OrderBook
from .instruments import Instrument
from .publisher import Publisher

log = logging.getLogger("md")

TRADE = "2"  # MDEntryType(269)
AGGRESSOR = {"1": "BUY", "2": "SELL"}
DEPTH_LEVELS = 10


def _px(entry: dict[int, str]) -> float | None:
    return float(entry[270]) if entry.get(270) else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class MarketDataApp:
    """Lists instruments on logon, subscribes to them, keeps their books and publishes every change.

    `symbols` limits the subscription; empty means every instrument the exchange lists.
    """

    def __init__(self, publisher: Publisher, symbols: Collection[str] = ()):
        self.publisher = publisher
        self.wanted = set(symbols)
        self.books: dict[str, OrderBook] = {}
        self.instruments: dict[str, Instrument] = {}
        self.last_trades: dict[str, dict[str, Any]] = {}
        self._listing: list[Instrument] = []
        self._ids = itertools.count(1)

    # ---- SessionApp ------------------------------------------------------------------------

    def on_logon(self, session: FixSession) -> None:
        for book in self.books.values():
            book.clear()
            book.stale = True
        self._listing = []
        session.send("x", [(320, f"SEC-{next(self._ids)}"), (559, "4")])  # 559=4: all securities

    def on_disconnect(self, session: FixSession) -> None:
        for book in self.books.values():
            book.stale = True
            self._publish(book)

    def on_app_message(self, session: FixSession, msg: FixMessage) -> None:
        mt = msg.msg_type
        if mt == "y":
            self._on_security_list(session, msg)
        elif mt == "W":
            self._on_snapshot(msg)
        elif mt == "X":
            self._on_incremental(msg)
        elif mt == "Y":
            log.error("market data request %s rejected: %s", msg.get(262), msg.get(58))
        elif mt == "j":
            log.error("request %s rejected: %s", msg.get(379), msg.get(58))

    # ---- handlers --------------------------------------------------------------------------

    def _on_security_list(self, session: FixSession, msg: FixMessage) -> None:
        if msg.get(560, "0") not in ("0", "2"):
            log.error("security list request %s failed: SecurityRequestResult=%s", msg.get(320), msg.get(560))
        self._listing += [Instrument.from_security_list(e) for e in msg.groups(146, 55)]
        if msg.get(893, "Y") != "Y":
            return  # more fragments to come
        listed = {i.symbol: i for i in self._listing}
        for missing in sorted(self.wanted - listed.keys()):
            log.warning("%s is not listed by the exchange", missing)
        for symbol, inst in listed.items():
            if self.wanted and symbol not in self.wanted:
                continue
            self.instruments[symbol] = inst
            self.publisher.instrument(symbol, inst.to_dict())
            self.books.setdefault(symbol, OrderBook(symbol))
            session.send(
                "V",
                [
                    (262, f"MD-{next(self._ids)}"),
                    (263, "1"),  # snapshot + updates
                    (264, "0"),  # full depth
                    (267, "3"),
                    (269, BID),
                    (269, OFFER),
                    (269, TRADE),
                    (146, "1"),
                    (55, symbol),
                ],
            )
        log.info("subscribed to %d instruments", len(self.books))

    def _on_snapshot(self, msg: FixMessage) -> None:
        book = self.books.get(msg.get(55, ""))
        if book is None:
            return
        book.clear()
        for i, entry in enumerate(msg.groups(268, 269)):
            book.apply(NEW, entry.get(278) or f"W{i}", entry.get(269, ""), _px(entry), float(entry.get(271) or 0))
            self._track_state(book.symbol, entry)
        book.stale = False
        log.info("snapshot %r", book)
        self._publish(book)

    def _on_incremental(self, msg: FixMessage) -> None:
        touched: dict[str, OrderBook] = {}
        for entry in msg.groups(268, 279):
            book = self.books.get(entry.get(55, ""))
            if book is None or book.stale:
                continue  # ignore increments until the snapshot arrives
            if entry.get(269) == TRADE:
                self.last_trades[book.symbol] = {
                    "price": _px(entry),
                    "qty": float(entry.get(271) or 0),
                    "aggressor": AGGRESSOR.get(entry.get(2446, ""), ""),
                    "trade_id": entry.get(1003, ""),
                    "time": _now(),
                }
            else:
                book.apply(entry[279], entry.get(278, ""), entry.get(269, ""), _px(entry), float(entry.get(271) or 0))
            self._track_state(book.symbol, entry)
            touched[book.symbol] = book
        for book in touched.values():
            self._publish(book)

    def _track_state(self, symbol: str, entry: dict[int, str]) -> None:
        state = entry.get(336)
        inst = self.instruments.get(symbol)
        if state and inst is not None and inst.state != state:
            log.info("%s is %s", symbol, state)
            inst.state = state
            self.publisher.instrument(symbol, inst.to_dict())

    # ---- output ----------------------------------------------------------------------------

    def payload(self, book: OrderBook) -> dict[str, Any]:
        bids, asks = book.depth(BID, DEPTH_LEVELS), book.depth(OFFER, DEPTH_LEVELS)
        return {
            "symbol": book.symbol,
            "stale": book.stale,
            "bid": bids[0][0] if bids else None,
            "bid_qty": bids[0][1] if bids else None,
            "ask": asks[0][0] if asks else None,
            "ask_qty": asks[0][1] if asks else None,
            "mid": book.mid(),
            "bids": bids,
            "asks": asks,
            "last_trade": self.last_trades.get(book.symbol),
            "time": _now(),
        }

    def _publish(self, book: OrderBook) -> None:
        self.publisher.book(book.symbol, self.payload(book))
