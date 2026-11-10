"""The market data session app: reference data, subscriptions, one OrderBook per symbol, publishing.

Besides the book and trades, it keeps each contract's state (TradingSessionID(336): OPEN, EXPIRED, ...)
and its settlement price (MDEntryType(269)=6), and publishes both with the book.

ElectronX lists new hourly contracts all the time, so the instrument list is requested again every few
minutes (request_list) and only new contracts are subscribed. A contract that drops off the list stays
subscribed until its settlement price has arrived (PJM settles the next day), `keep_unsettled_s` has
passed, or the exchange rejects the subscription; then it is retired: unsubscribed, forgotten here, and
its Redis keys left to expire.

Subscriptions go out at most `subscribe_per_s` a second: the exchange throttles a session that sends
too fast, and a burst of thousands of requests at every logon would look like runaway activity. They go
in order of interest, as on other trading platforms:
  1. contracts on someone's screen (set_interest: the api publishes what its browsers show)
  2. contracts the account holds (their marks and settlement prices are needed for risk)
  3. in the background (unless turned off), every other contract still trading, soonest end first
A contract whose trading has ended is subscribed only if it is on a screen or held.

The Snapshot API (snapshot.py), when configured, adds the exchange's marks (the day-ahead price) to each
book, and its settlement prices stand in until FIX market data sends one.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from trading_core import Symbol
from trading_fix import FixMessage, FixSession

from .book import BID, NEW, OFFER, OrderBook
from .instruments import Instrument
from .publisher import Publisher

log = logging.getLogger("md")

TRADE, SETTLEMENT = "2", "6"  # MDEntryType(269)
INDICATIVE = {"3", "5"}  # OpenCloseSettlFlag(286): expected entry, theoretical price; anything else is final
AGGRESSOR = {"1": "BUY", "2": "SELL"}
DEPTH_LEVELS = 10
KEEP_UNSETTLED_S = 3 * 24 * 3600
SUBSCRIBE_PER_S = 50
ENDED_STATES = {"EXPIRED", "TERMINATED"}  # TradingSessionID(336)


def _end_of_trading(symbol: str, inst: Instrument) -> float:
    """When trading in the contract stops: the exchange's last trade time, else the end of its hour."""
    if inst.last_trade_time:
        try:
            return datetime.fromisoformat(inst.last_trade_time).timestamp()
        except ValueError:
            pass
    try:
        return Symbol.parse(symbol).expires_at.timestamp()
    except ValueError:
        return float("inf")  # not an ElectronX symbol: never ends


SAME_PRICE = 0.01  # FIX and the Snapshot API may round a settlement price differently


def _px(entry: dict[int, str]) -> float | None:
    return float(entry[270]) if entry.get(270) else None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _entry_time(entry: dict[int, str]) -> str:
    """MDEntryDate(272) and MDEntryTime(273), both UTC, as ISO 8601; now if they are missing."""
    try:
        when = datetime.strptime(f"{entry[272]}-{entry[273][:15]}", "%Y%m%d-%H:%M:%S.%f")
    except KeyError, ValueError:
        return _now()
    return when.replace(tzinfo=UTC).isoformat(timespec="milliseconds")


class MarketDataApp:
    """Lists instruments on logon and every few minutes, subscribes to new ones, keeps their books and
    publishes every change.

    `symbols` limits the subscription; empty means every instrument the exchange lists.
    """

    def __init__(
        self,
        publisher: Publisher,
        symbols: Collection[str] = (),
        keep_unsettled_s: float = KEEP_UNSETTLED_S,
        clock: Callable[[], float] = time.time,
        subscribe_per_s: int = SUBSCRIBE_PER_S,
        background: bool = True,
    ):
        self.publisher = publisher
        self.wanted = set(symbols)
        self.keep_unsettled_s = keep_unsettled_s
        self.clock = clock
        self.books: dict[str, OrderBook] = {}
        self.instruments: dict[str, Instrument] = {}
        self.last_trades: dict[str, dict[str, Any]] = {}
        self.settlements: dict[str, dict[str, Any]] = {}
        self.snapshot_marks: dict[str, dict[str, Any]] = {}  # symbol -> the Snapshot API's marks
        self.subscribed: dict[str, str] = {}  # symbol -> MDReqID, on this connection
        self.delisted: dict[str, float] = {}  # symbol -> when it left the exchange's list
        self._requests: dict[str, str] = {}  # MDReqID -> symbol
        self._listing: list[Instrument] = []
        self._list_req = ""
        self._ids = itertools.count(1)
        self._disagree: set[str] = set()  # symbols whose two settlement prices differ, already reported
        self.subscribe_per_s = subscribe_per_s
        self.background = background  # subscribe to every contract still trading, after the ones of interest
        self.on_screen: set[str] = set()  # contracts some browser shows
        self.held: set[str] = set()  # contracts the account holds
        self._still_trading: list[str] = []  # soonest end of trading first
        self._next_batch: asyncio.TimerHandle | None = None
        self._session: FixSession | None = None

    # ---- SessionApp ------------------------------------------------------------------------

    def on_logon(self, session: FixSession) -> None:
        self._session = session
        for book in self.books.values():
            book.clear()
            book.stale = True
        self.subscribed.clear()  # a new connection starts with no subscriptions
        self._stop_subscribing()
        self.request_list(session)

    def on_disconnect(self, session: FixSession) -> None:
        self._stop_subscribing()
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
        elif mt in ("Y", "j"):
            self._on_reject(session, msg.get(262) if mt == "Y" else msg.get(379), msg.get(58))

    def request_list(self, session: FixSession) -> None:
        """Ask for every listed instrument (on logon, then every few minutes)."""
        self._list_req = f"SEC-{next(self._ids)}"
        self._listing = []
        session.send("x", [(320, self._list_req), (559, "4")])  # 559=4: all securities

    # ---- Snapshot API ----------------------------------------------------------------------

    def markets(self) -> list[str]:
        """The ISOs of the contracts followed, which is what the Snapshot API is asked about."""
        found = set()
        for symbol in self.books:
            try:
                found.add(Symbol.parse(symbol).market.value)
            except ValueError:
                continue
        return sorted(found)

    def on_marks(self, marks: Mapping[str, dict[str, Any]]) -> None:
        """The exchange's marks per contract, e.g. {"dam": {"price": 27.84, "time": ...}}."""
        for symbol, kinds in marks.items():
            book = self.books.get(symbol)
            if book is not None and self.snapshot_marks.get(symbol) != kinds:
                self.snapshot_marks[symbol] = kinds
                self._publish(book)

    def on_settlements(self, settlements: Mapping[str, dict[str, Any]]) -> None:
        """Settlement prices from the Snapshot API: used until FIX market data sends a final one."""
        for symbol, new in settlements.items():
            book = self.books.get(symbol)
            if book is None:
                continue
            current = self.settlements.get(symbol)
            if current is not None and current["final"]:
                self._check_same(symbol, current, new["price"])
                continue
            self.settlements[symbol] = {
                "price": new["price"],
                "final": True,
                "flag": "",
                "source": "snapshot",
                "time": new["time"] or _now(),
            }
            log.info("%s settlement %s (Snapshot API)", symbol, new["price"])
            self._publish(book)

    def _check_same(self, symbol: str, current: dict[str, Any], other: float) -> None:
        if abs(current["price"] - other) > SAME_PRICE and symbol not in self._disagree:
            self._disagree.add(symbol)
            price, source = current["price"], current["source"]
            log.warning("%s settlement differs: %s from %s, %s from the other source", symbol, price, source, other)

    # ---- handlers --------------------------------------------------------------------------

    def _on_security_list(self, session: FixSession, msg: FixMessage) -> None:
        if msg.get(320) != self._list_req:
            return  # the answer to an older request
        if msg.get(560, "0") not in ("0", "2"):
            log.error("security list request %s failed: SecurityRequestResult=%s", msg.get(320), msg.get(560))
            return
        self._listing += [Instrument.from_security_list(e) for e in msg.group_fields(146, 55)]
        if msg.get(893, "Y") != "Y":
            return  # more fragments to come
        listed = {i.symbol: i for i in self._listing if not self.wanted or i.symbol in self.wanted}
        for missing in sorted(self.wanted - listed.keys()):
            log.warning("%s is not listed by the exchange", missing)
        now = self.clock()
        for symbol, inst in listed.items():
            self.delisted.pop(symbol, None)
            old = self.instruments.get(symbol)
            if old is not None:
                inst.state = old.state  # the list carries no state; market data does
            if old != inst:
                self.instruments[symbol] = inst
                self.publisher.instrument(symbol, inst.to_dict())
        for symbol in self.instruments.keys() - listed.keys():
            self.delisted.setdefault(symbol, now)
        for symbol, since in list(self.delisted.items()):
            final = (self.settlements.get(symbol) or {}).get("final")
            if final or now - since >= self.keep_unsettled_s:
                self._retire(session, symbol, "settled" if final else "never settled")
        ends = {s: _end_of_trading(s, i) for s, i in self.instruments.items() if i.state not in ENDED_STATES}
        self._still_trading = sorted((s for s, end in ends.items() if end > now), key=lambda s: ends[s])
        log.info(
            "%d instruments listed, %d still trading%s",
            len(self.instruments),
            len(self._still_trading),
            "" if self.background else " (subscribing only to those on a screen or held)",
        )
        self._kick(session)

    def set_interest(self, on_screen: Collection[str], held: Collection[str]) -> None:
        """Contracts some browser shows, and contracts the account holds: subscribed before any other."""
        on_screen, held = set(on_screen), set(held)
        if (on_screen, held) == (self.on_screen, self.held):
            return
        self.on_screen, self.held = on_screen, held
        if self._session is not None:
            self._kick(self._session)

    def _kick(self, session: FixSession) -> None:
        if self._next_batch is None and session.logged_on:
            self._subscribe_batch(session)

    def _next_symbols(self, n: int) -> list[str]:
        """Up to `n` contracts to subscribe to next, the most interesting first."""
        out: list[str] = []
        taken: set[str] = set()
        for group in (sorted(self.on_screen), sorted(self.held), self._still_trading if self.background else ()):
            for symbol in group:
                if symbol in self.instruments and symbol not in self.subscribed and symbol not in taken:
                    out.append(symbol)
                    taken.add(symbol)
                    if len(out) == n:
                        return out
        return out

    def _subscribe_batch(self, session: FixSession) -> None:
        """Subscribe to the next `subscribe_per_s` contracts; look for more a second later."""
        self._next_batch = None
        if not session.logged_on:
            return
        batch = self._next_symbols(self.subscribe_per_s)
        for symbol in batch:
            self._subscribe(session, symbol)
        if batch:
            self._next_batch = asyncio.get_running_loop().call_later(1.0, self._subscribe_batch, session)
            if len(batch) < self.subscribe_per_s:
                log.info("subscribed to %d instruments", len(self.subscribed))

    def _stop_subscribing(self) -> None:
        if self._next_batch is not None:
            self._next_batch.cancel()
            self._next_batch = None

    def _subscribe(self, session: FixSession, symbol: str) -> None:
        req_id = f"MD-{next(self._ids)}"
        self.subscribed[symbol] = req_id
        self._requests[req_id] = symbol
        self.books.setdefault(symbol, OrderBook(symbol))
        session.send(
            "V",
            [
                (262, req_id),
                (263, "1"),  # snapshot + updates
                (264, "0"),  # full depth
                (267, "4"),
                (269, BID),
                (269, OFFER),
                (269, TRADE),
                (269, SETTLEMENT),
                (146, "1"),
                (55, symbol),
            ],
        )

    def _retire(self, session: FixSession, symbol: str, why: str) -> None:
        """Stop following a contract that has left the list."""
        req_id = self.subscribed.pop(symbol, None)
        if req_id is not None:
            session.send("V", [(262, req_id), (263, "2"), (264, "0"), (146, "1"), (55, symbol)])  # unsubscribe
        tables = (self.books, self.instruments, self.last_trades, self.settlements, self.snapshot_marks, self.delisted)
        for table in tables:
            table.pop(symbol, None)
        self._disagree.discard(symbol)
        self.publisher.retire(symbol)
        log.info("%s retired (%s)", symbol, why)

    def _on_reject(self, session: FixSession, req_id: str | None, text: str | None) -> None:
        symbol = self._requests.get(req_id or "")
        if symbol is not None and (symbol in self.delisted or symbol not in self.instruments):
            if symbol in self.subscribed:
                self.subscribed.pop(symbol)
                self._retire(session, symbol, f"no longer at the exchange: {text}")
            return
        log.error("request %s rejected: %s", req_id, text)

    def _on_snapshot(self, msg: FixMessage) -> None:
        book = self.books.get(msg.get(55, ""))
        if book is None:
            return
        book.clear()
        for i, entry in enumerate(msg.groups(268, 269)):
            if entry.get(269) == SETTLEMENT:
                self._settlement(book.symbol, entry)
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
            elif entry.get(269) == SETTLEMENT:
                if entry.get(279) != "2":  # not a delete
                    self._settlement(book.symbol, entry)
            else:
                book.apply(entry[279], entry.get(278, ""), entry.get(269, ""), _px(entry), float(entry.get(271) or 0))
            self._track_state(book.symbol, entry)
            touched[book.symbol] = book
        for book in touched.values():
            self._publish(book)

    def _settlement(self, symbol: str, entry: dict[int, str]) -> None:
        price = _px(entry)
        if price is None:
            return
        flag = entry.get(286, "")
        final = flag not in INDICATIVE
        previous = self.settlements.get(symbol)
        if previous is not None and previous["final"]:
            if not final or abs(previous["price"] - price) <= SAME_PRICE:
                return  # the same price again (a new snapshot, or the Snapshot API had it first): keep its time
            if previous["source"] == "snapshot":
                self._check_same(symbol, previous, price)
        self.settlements[symbol] = {
            "price": price,
            "final": final,
            "flag": flag,
            "source": "fix",
            "time": _entry_time(entry),
        }
        log.info("%s settlement %s (%s)", symbol, price, "final" if final else "indicative")

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
        inst = self.instruments.get(book.symbol)
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
            "state": inst.state if inst else "",
            "settlement": self.settlements.get(book.symbol),  # {price, final, flag, source, time} or None
            "marks": self.snapshot_marks.get(book.symbol),  # the exchange's, e.g. {"dam": {price, time}}, or None
            "time": _now(),
        }

    def _publish(self, book: OrderBook) -> None:
        self.publisher.book(book.symbol, self.payload(book))
