"""Market data side of the simulated gateway: SecurityList, snapshots and a moving market-by-order book.

Messages follow the ElectronX layouts:
  * SecurityListRequest [x] -> SecurityList [y] with tick size, lot size and price limits
  * MarketDataRequest [V] -> snapshot [W], then incremental refreshes [X] while subscribed
  * every resting order is its own entry, keyed by MDEntryID(278); trades are MDEntryType(269)=2

The book is shared with the simulated order entry gateway, so client orders show up in market data
and trade against the synthetic liquidity.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from trading_fix import FixMessage

from .gateway import Fields, SimGateway, SimSession

log = logging.getLogger("sim.md")

BID, OFFER, TRADE = "0", "1", "2"  # MDEntryType(269)
NEW, CHANGE, DELETE = "0", "1", "2"  # MDUpdateAction(279)
BUY, SELL = "1", "2"  # Side / AggressorSide(2446)


@dataclass
class SimInstrument:
    symbol: str
    fair: float = 45.0
    tick: float = 0.25
    lot: int = 1
    low: float = 0.0
    high: float = 500.0
    state: str = "OPEN"


@dataclass
class RestingOrder:
    order_id: str
    side: str  # BID or OFFER
    ticks: int
    qty: int
    owner: str | None = None  # SenderCompID of a client order; None for the simulator's own liquidity


#: Called when a client's resting order is filled: (symbol, order, filled qty, price in ticks).
FillListener = Callable[[str, RestingOrder, int, int], None]


def _now_parts() -> tuple[str, str]:
    now = datetime.now(UTC)
    return now.strftime("%Y%m%d"), now.strftime("%H:%M:%S.%f")


class SimMarketData:
    """Attach to a SimGateway to serve reference data and market data with synthetic liquidity."""

    BOOK_LEVELS = 5

    def __init__(self, gateway: SimGateway, instruments: list[SimInstrument], seed: int | None = None):
        self.gateway = gateway
        gateway.on_app_message = self._on_message
        self.instruments = {i.symbol: i for i in instruments}
        self.books: dict[str, dict[str, RestingOrder]] = {i.symbol: {} for i in instruments}
        self.fair: dict[str, int] = {i.symbol: round(i.fair / i.tick) for i in instruments}
        self.subscriptions: dict[str, dict[str, str]] = {}  # SenderCompID -> {symbol: MDReqID}
        self.on_fill: FillListener | None = None
        self.rng = random.Random(seed)
        self._ids = itertools.count(1)
        self._run = format(time.time_ns() // 1_000_000, "x")  # keeps ids unique across simulator restarts
        self._pending: dict[str, list[Fields]] = {}
        for symbol in self.books:
            for k in range(1, self.BOOK_LEVELS + 1):
                self.add_order(symbol, BID, self.fair[symbol] - k, self.rng.randint(1, 10))
                self.add_order(symbol, OFFER, self.fair[symbol] + k, self.rng.randint(1, 10))
        self._pending.clear()  # nobody is subscribed yet

    def new_id(self, prefix: str) -> str:
        """OrderIDs, ExecIDs and trade ids, unique like the real exchange's: downstream services
        deduplicate fills by ExecID, so a restarted simulator must never reuse one."""
        return f"{prefix}{self._run}-{next(self._ids)}"

    # ---- book queries ----------------------------------------------------------------------

    def best_ticks(self, symbol: str, side: str) -> int | None:
        prices = [o.ticks for o in self.books[symbol].values() if o.side == side]
        if not prices:
            return None
        return max(prices) if side == BID else min(prices)

    def best(self, symbol: str, side: str) -> float | None:
        ticks = self.best_ticks(symbol, side)
        return None if ticks is None else ticks * self.instruments[symbol].tick

    def oldest_at(self, symbol: str, side: str, ticks: int) -> RestingOrder | None:
        return next((o for o in self.books[symbol].values() if o.side == side and o.ticks == ticks), None)

    # ---- book changes (also used directly by tests) ----------------------------------------

    def add_order(
        self, symbol: str, side: str, ticks: int, qty: int, owner: str | None = None, order_id: str | None = None
    ) -> str:
        order = RestingOrder(order_id or self.new_id("O"), side, ticks, qty, owner)
        self.books[symbol][order.order_id] = order
        self._entry(symbol, NEW, order)
        return order.order_id

    def cancel_order(self, symbol: str, order_id: str) -> None:
        order = self.books[symbol].pop(order_id)
        self._entry(symbol, DELETE, order)

    def execute(self, symbol: str, resting: RestingOrder, qty: int, aggressor: str) -> int:
        """Fill `qty` of a resting order against an incoming aggressor; returns the price in ticks."""
        resting.qty -= qty
        if resting.qty == 0:
            del self.books[symbol][resting.order_id]
            self._entry(symbol, DELETE, resting)
        else:
            self._entry(symbol, CHANGE, resting)
        trade_id = self.new_id("T")
        d, t = _now_parts()
        self._pending.setdefault(symbol, []).append(
            [
                (279, NEW),
                (269, TRADE),
                (278, trade_id + "P"),
                (55, symbol),
                (270, self.fmt(symbol, resting.ticks)),
                (271, str(qty)),
                (272, d),
                (273, t),
                (1003, trade_id),
                (2446, aggressor),
            ]
        )
        if resting.owner is not None and self.on_fill is not None:
            self.on_fill(symbol, resting, qty, resting.ticks)
        return resting.ticks

    def trade(self, symbol: str, aggressor: str, qty: int) -> float | None:
        """An outside taker hits the best bid (aggressor SELL) or lifts the best offer (aggressor BUY)."""
        side = OFFER if aggressor == BUY else BID
        best_ticks = self.best_ticks(symbol, side)
        if best_ticks is None:
            return None
        order = self.oldest_at(symbol, side, best_ticks)
        assert order is not None
        self.execute(symbol, order, min(qty, order.qty), aggressor)
        return best_ticks * self.instruments[symbol].tick

    def move_fair(self, symbol: str, ticks: int) -> None:
        """Shift fair value and pull any of the simulator's own orders that would now cross it."""
        self.fair[symbol] += ticks
        fair = self.fair[symbol]
        for order in list(self.books[symbol].values()):
            if order.owner is not None:
                continue  # client orders stay until they trade or are cancelled
            if (order.side == BID and order.ticks >= fair) or (order.side == OFFER and order.ticks <= fair):
                self.cancel_order(symbol, order.order_id)

    def _quote(self, symbol: str, side: str, ticks: int, qty: int) -> None:
        """Add synthetic liquidity, or trade if the price would cross a resting order."""
        opposite = OFFER if side == BID else BID
        best = self.best_ticks(symbol, opposite)
        crosses = best is not None and (ticks >= best if side == BID else ticks <= best)
        if crosses:
            self.trade(symbol, BUY if side == BID else SELL, qty)
        else:
            self.add_order(symbol, side, ticks, qty)

    def step(self) -> None:
        """One random change per instrument, then send the batched updates."""
        for symbol in self.books:
            r = self.rng.random()
            synthetic = [o for o in self.books[symbol].values() if o.owner is None]
            if r < 0.35:
                side = self.rng.choice((BID, OFFER))
                k = self.rng.randint(1, 6)
                ticks = self.fair[symbol] - k if side == BID else self.fair[symbol] + k
                self._quote(symbol, side, ticks, self.rng.randint(1, 10))
            elif r < 0.6 and synthetic:
                self.cancel_order(symbol, self.rng.choice(synthetic).order_id)
            elif r < 0.8:
                self.trade(symbol, self.rng.choice((BUY, SELL)), self.rng.randint(1, 3))
            else:
                self.move_fair(symbol, self.rng.choice((-1, 1)))
            for side in (BID, OFFER):  # keep both sides populated
                if sum(1 for o in self.books[symbol].values() if o.side == side) < 2:
                    k = self.rng.randint(1, 3)
                    ticks = self.fair[symbol] - k if side == BID else self.fair[symbol] + k
                    self._quote(symbol, side, ticks, self.rng.randint(1, 10))
        self.flush()

    async def run(self, interval_s: float = 0.5) -> None:
        while True:
            await asyncio.sleep(interval_s)
            self.step()

    def flush(self) -> None:
        """Send the batched entries as one IncrementalRefresh [X] per symbol to each subscriber."""
        pending, self._pending = self._pending, {}
        for symbol, entries in pending.items():
            for sender, subs in self.subscriptions.items():
                req_id = subs.get(symbol)
                session = self.gateway.sessions.get(sender)
                if req_id is None or session is None or session.conn is None:
                    continue
                body: Fields = [(262, req_id), (268, str(len(entries)))]
                for entry in entries:
                    body += entry
                session.send("X", body)

    # ---- requests from clients -------------------------------------------------------------

    def _on_message(self, session: SimSession, msg: FixMessage) -> None:
        if msg.msg_type == "x":
            self._security_list(session, msg)
        elif msg.msg_type == "V":
            self._market_data_request(session, msg)
        else:
            business_reject(session, msg, 3, "Unsupported Message Type")

    def _security_list(self, session: SimSession, msg: FixMessage) -> None:
        if msg.get(559) == "4":  # all securities
            insts = list(self.instruments.values())
        else:
            insts = [i for s, i in self.instruments.items() if s == msg.get(55)]
        if msg.get(336):  # TradingSessionID filters on instrument state
            insts = [i for i in insts if i.state == msg[336]]
        body: Fields = [(320, msg.get(320, "")), (560, "0" if insts else "2"), (893, "Y"), (146, str(len(insts)))]
        for i in insts:
            body += [
                (55, i.symbol),
                (48, i.symbol),
                (22, "8"),
                (167, "EVENT"),
                (969, str(i.tick)),
                (562, str(i.lot)),
                (1148, f"{i.low:.2f}"),
                (1149, f"{i.high:.2f}"),
                (15, "USD"),
            ]
        session.send("y", body)

    def _market_data_request(self, session: SimSession, msg: FixMessage) -> None:
        symbols = [e[55] for e in msg.groups(146, 55)] or [msg.get(55, "")]
        req_id, sub_type = msg.get(262, ""), msg.get(263, "0")
        subs = self.subscriptions.setdefault(session.sender, {})
        for symbol in symbols:
            if symbol not in self.books:
                business_reject(session, msg, 2, f"Unknown security {symbol}")
            elif sub_type == "2":  # unsubscribe
                subs.pop(symbol, None)
            else:
                self._snapshot(session, symbol, req_id)
                if sub_type == "1":
                    subs[symbol] = req_id

    def _snapshot(self, session: SimSession, symbol: str, req_id: str) -> None:
        inst = self.instruments[symbol]
        orders = sorted(self.books[symbol].values(), key=lambda o: (o.side, -o.ticks if o.side == BID else o.ticks))
        body: Fields = [(55, symbol), (262, req_id), (268, str(len(orders)))]
        for o in orders:
            body += [
                (269, o.side),
                (270, self.fmt(symbol, o.ticks)),
                (271, str(o.qty)),
                (278, o.order_id),
                (336, inst.state),
            ]
        session.send("W", body)

    # ---- helpers ---------------------------------------------------------------------------

    def _entry(self, symbol: str, action: str, order: RestingOrder) -> None:
        inst = self.instruments[symbol]
        d, t = _now_parts()
        self._pending.setdefault(symbol, []).append(
            [
                (279, action),
                (269, order.side),
                (278, order.order_id),
                (55, symbol),
                (270, self.fmt(symbol, order.ticks)),
                (271, str(0 if action == DELETE else order.qty)),
                (272, d),
                (273, t),
                (336, inst.state),
            ]
        )

    def fmt(self, symbol: str, ticks: int) -> str:
        return f"{ticks * self.instruments[symbol].tick:.2f}"


def business_reject(session: SimSession, msg: FixMessage, reason: int, text: str) -> None:
    ref_id = msg.get(11) or msg.get(262) or msg.get(320) or ""
    session.send("j", [(45, msg.get(34, "0")), (58, text), (372, msg.msg_type), (379, ref_id), (380, str(reason))])
