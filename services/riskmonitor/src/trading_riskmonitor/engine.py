"""Risk per trader and for the desk, from the exchange's fills and live marks.

Fills come from dropcopy (the exchange's record) and carry a ClOrdID but no trader; the trader is
found from orderentry's `orders` stream. A fill whose order is not known yet waits until it is. If it
is still unknown after SCREEN_AFTER_S, the order was entered on the ElectronX screen: the fill goes to
the trader whose ElectronX trader id (electronx.traders.exchange_trader_id) is the fill's `user`, the
TargetSubID(57) on the drop copy. A fill whose user matches no trader stays unattributed.

A trade bust (a fill with `bust` set) undoes its fill: it is applied as the opposite trade at the
same price, which takes the position back and leaves the P&L as if the trade had never happened.

P&L is reported for today's trading day (see day.py) and since start (every fill riskmonitor has
seen). Today's P&L values what was held when the day began at the start-of-day mark, so
    today = (position now x mark now) - (position at the start x start-of-day mark) - cost of today's fills
The start-of-day mark is each contract's mark at the moment the day rolled. It cannot be rebuilt from
fills, so redis_io keeps it in Redis and riskmonitor reloads it after a restart. A position with no
start-of-day mark (riskmonitor was down at the roll, or the contract had no mark yet) takes the first
mark it gets, flagged `sod_mark_late`.

A contract stops trading at its last trade time from the exchange's SecurityList, else at the end of
its hour in the ISO's time zone (Symbol.expires_at), or when market data says it is EXPIRED. Its book
is dead from then on, so it keeps its last mark, flagged `expired`, until the exchange publishes a
settlement price on market data. A final settlement price closes the position at that price: its P&L
becomes realized and it no longer counts against position limits. An indicative one is only a mark.

Limits come from the trading rules in SQL Server (set_rules): each trader's max loss for the day, and
the max position of each contract. A trader with no loss limit set shows no usage and is never blocked
here; orderentry rejects their orders anyway, since its rules fail closed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trading_core import Symbol, TradingRules

from .day import TradingDay
from .marks import Mark, choose_mark
from .positions import Position

log = logging.getLogger("risk")


ENDED_STATES = {"EXPIRED", "TERMINATED"}  # TradingSessionID(336)
SCREEN_AFTER_S = 5.0  # a fill whose order orderentry has not published by then came from the ElectronX screen


def _iso_time(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except TypeError, ValueError:
        return None


def _pct(used: float, limit: float) -> float:
    return round(100.0 * used / limit, 1) if limit else 0.0


def _signed(side: str, qty: int) -> int:
    return qty if side == "BUY" else -qty


def fix_time(value: str) -> float | None:
    """TransactTime(60), e.g. 20261110-14:03:56.123 (UTC), as seconds since the epoch."""
    for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Settlement:
    price: float
    final: bool  # False: indicative (OpenCloseSettlFlag 3 or 5)
    time: float  # when the exchange published it


@dataclass(frozen=True)
class SodMark:
    price: float
    late: bool = False  # taken after the day began, not at the roll


@dataclass
class Holding:
    """One trader's dealings in one contract."""

    since_start: Position = field(default_factory=Position)
    sod_qty: int = 0  # lots held when today's trading day began
    today: list[tuple[str, int, float]] = field(default_factory=list)  # today's fills: side, qty, price
    screen: int = 0  # the part of the position traded on the ElectronX screen, in lots

    def today_position(self, sod_price: float | None) -> Position:
        """Today's position: what was held at the start, bought at the start-of-day mark, then today's
        fills. Without a start-of-day mark only today's own fills count."""
        pos = Position(self.sod_qty, sod_price) if self.sod_qty and sod_price is not None else Position()
        for side, qty, price in self.today:
            pos.apply(side, qty, price)
        return pos

    def roll(self) -> None:
        self.sod_qty += sum(_signed(side, qty) for side, qty, _ in self.today)
        self.today.clear()


def _pnl(realized: float, unrealized: float) -> dict[str, float]:
    return {"realized": realized, "unrealized": unrealized, "total": realized + unrealized}


def _add(into: dict[str, float], pnl: Mapping[str, float]) -> None:
    for k in ("realized", "unrealized", "total"):
        into[k] += pnl[k]


def _rounded(pnl: Mapping[str, float]) -> dict[str, float]:
    return {k: round(v, 2) for k, v in pnl.items()}


class RiskEngine:
    def __init__(
        self,
        rules: TradingRules | None = None,
        max_spread: float = 5.0,  # USD/MWh; wider books are not marked at their mid
        clock: Callable[[], float] = time.time,
        trading_day: TradingDay | None = None,
    ):
        self.rules = rules
        self.max_spread = max_spread
        self.clock = clock
        self.trading_day = trading_day or TradingDay()
        self.today = self.trading_day.day_of(clock())
        self.traders: dict[str, str] = {}  # ClOrdID, and the exchange's OrderID once known -> trader
        self.holdings: dict[tuple[str, str], Holding] = {}  # (trader, symbol) -> holding
        self.books: dict[str, dict[str, Any]] = {}
        self.instruments: dict[str, dict[str, Any]] = {}
        self.marks: dict[str, Mark] = {}
        self.settlements: dict[str, Settlement] = {}
        self.sod_marks: dict[str, SodMark] = {}
        self.sod_version = 0  # goes up whenever sod_marks change, so they get saved
        self._expiry: dict[str, float | None] = {}
        self._waiting: dict[str, list[dict[str, Any]]] = {}  # ClOrdID -> fills whose order is not known yet
        self._seen: set[str] = set()  # ExecIDs already applied

    # ---- inputs ----------------------------------------------------------------------------

    def on_order(self, order: Mapping[str, Any]) -> None:
        cl = order["cl_ord_id"]
        self.traders[cl] = order["trader"]
        if order.get("order_id"):  # a fill after a cancel request carries the cancel's ClOrdID instead
            self.traders[order["order_id"]] = order["trader"]
        for fill in self._waiting.pop(cl, []):
            self._apply(order["trader"], fill)

    def on_fill(self, fill: Mapping[str, Any], received: float | None = None) -> None:
        """A fill from dropcopy. Its trading day comes from its TransactTime, else from `received`
        (when it reached Redis), else from now."""
        if fill["exec_id"] in self._seen:
            return
        self._seen.add(fill["exec_id"])
        fill = {**fill, "ts": fix_time(fill.get("time") or "") or received or self.clock(), "arrived": self.clock()}
        trader = self.traders.get(fill["cl_ord_id"]) or self.traders.get(fill.get("order_id") or "")
        if trader is None:
            self._waiting.setdefault(fill["cl_ord_id"], []).append(fill)
        else:
            self._apply(trader, fill)

    def on_book(self, book: Mapping[str, Any]) -> None:
        self._roll()  # a new day's start-of-day marks are the marks before this update
        self.books[book["symbol"]] = dict(book)
        settlement = book.get("settlement")
        if settlement and settlement.get("price") is not None:
            when = _iso_time(settlement.get("time")) or self.clock()
            final = bool(settlement.get("final", True))
            self.settlements[book["symbol"]] = Settlement(float(settlement["price"]), final, when)
        self._remark(book["symbol"])

    def set_rules(self, rules: TradingRules) -> None:
        self.rules = rules

    def on_instrument(self, instrument: Mapping[str, Any]) -> None:
        self.instruments[instrument["symbol"]] = dict(instrument)

    def _apply(self, trader: str, fill: Mapping[str, Any], screen: bool = False) -> None:
        self._roll()
        side, qty, price = fill["side"], int(fill["qty"]), float(fill["price"])
        if fill.get("bust"):  # the exchange cancelled this trade: undo it, at its own price
            side = "SELL" if side == "BUY" else "BUY"
        holding = self.holdings.setdefault((trader, fill["symbol"]), Holding())
        if screen:
            holding.screen += _signed(side, qty)
        holding.since_start.apply(side, qty, price)
        if self.trading_day.day_of(fill["ts"]) < self.today:
            holding.sod_qty += _signed(side, qty)  # an earlier day's fill: part of the start-of-day position
        else:
            holding.today.append((side, qty, price))
        if fill["symbol"] not in self.marks:
            self._remark(fill["symbol"])

    def _remark(self, symbol: str) -> None:
        now = self.clock()
        previous = self.marks.get(symbol)
        settlement = self.settlements.get(symbol)
        if settlement is not None:
            source = "settlement" if settlement.final else "indicative settlement"
            self.marks[symbol] = Mark(settlement.price, source, settlement.time)
            return
        if self.expired(symbol, now) and previous is not None and previous.price is not None:
            return  # the book is dead: keep the last mark until settlement
        self.marks[symbol] = choose_mark(
            self.books.get(symbol), self.instruments.get(symbol), previous, now, self.max_spread
        )

    def trading_ends(self, symbol: str) -> float | None:
        """The exchange's last trade time, else its expiration time, else the end of the symbol's hour."""
        instrument = self.instruments.get(symbol) or {}
        for key in ("last_trade_time", "expiration_time"):
            ends = _iso_time(instrument.get(key))
            if ends is not None:
                return ends
        if symbol not in self._expiry:
            try:
                self._expiry[symbol] = Symbol.parse(symbol).expires_at.timestamp()
            except ValueError:
                self._expiry[symbol] = None  # not an ElectronX symbol: never expires
        return self._expiry[symbol]

    def expired(self, symbol: str, now: float) -> bool:
        if symbol in self.settlements:
            return True
        state = (self.books.get(symbol) or {}).get("state") or (self.instruments.get(symbol) or {}).get("state")
        ends = self.trading_ends(symbol)
        return state in ENDED_STATES or (ends is not None and now >= ends)

    def settled(self, symbol: str) -> Settlement | None:
        """The final settlement, if the exchange has published it."""
        settlement = self.settlements.get(symbol)
        return settlement if settlement is not None and settlement.final else None

    # ---- trading day -----------------------------------------------------------------------

    def _roll(self) -> None:
        """At the start of a new trading day: today's fills become the start-of-day position, and
        every contract's current mark becomes its start-of-day mark."""
        day = self.trading_day.day_of(self.clock())
        if day <= self.today:
            return
        for holding in self.holdings.values():
            holding.roll()
        self.today = day
        self.sod_marks = {s: SodMark(m.price) for s, m in self.marks.items() if m.price is not None}
        self.sod_version += 1
        log.info("trading day %s began; start-of-day marks taken for %d contracts", day, len(self.sod_marks))

    def _sod_mark(self, symbol: str) -> SodMark | None:
        if symbol not in self.sod_marks:
            mark = self.marks.get(symbol)
            if mark is None or mark.price is None:
                return None
            self.sod_marks[symbol] = SodMark(mark.price, late=True)
            self.sod_version += 1
        return self.sod_marks[symbol]

    def sod_state(self) -> dict[str, Any]:
        marks = {s: {"price": m.price, "late": m.late} for s, m in sorted(self.sod_marks.items())}
        return {"day": self.today.isoformat(), "marks": marks}

    def restore_sod(self, state: Mapping[str, Any]) -> bool:
        """Start-of-day marks saved before a restart; used only if they are for today."""
        self._roll()
        if state.get("day") != self.today.isoformat():
            return False
        self.sod_marks = {s: SodMark(float(m["price"]), bool(m.get("late"))) for s, m in state["marks"].items()}
        return True

    # ---- outputs ---------------------------------------------------------------------------

    def _attribute_screen_fills(self) -> None:
        """Fills of orders orderentry never published: give each to the trader its ElectronX user names."""
        if self.rules is None:
            return  # without the traders' ElectronX ids nobody can be found yet
        now = self.clock()
        for cl, fills in list(self._waiting.items()):
            if now - fills[0]["arrived"] < SCREEN_AFTER_S:
                continue
            trader = self.rules.trader_for(fills[0].get("user"))
            if trader is None:
                continue
            del self._waiting[cl]
            for fill in fills:
                self._apply(trader, fill, screen=True)
            log.info("%d fill(s) of %s on the ElectronX screen belong to %s", len(fills), cl, trader)

    def snapshot(self) -> dict[str, Any]:
        self._roll()
        self._attribute_screen_fills()
        now = self.clock()
        traders: dict[str, Any] = {}
        desk_positions: dict[str, int] = {}
        desk, desk_today = _pnl(0.0, 0.0), _pnl(0.0, 0.0)
        for (trader, symbol), h in sorted(self.holdings.items()):
            mark = self.marks.get(symbol)
            price = mark.price if mark else None
            since_start = _pnl(h.since_start.realized, h.since_start.unrealized(price))
            sod = self._sod_mark(symbol) if h.sod_qty else None
            day = h.today_position(sod.price if sod else None)
            today = _pnl(day.realized, day.unrealized(price))
            settled = self.settled(symbol)
            if settled is not None:  # closed at the settlement price: all of it is realized
                since_start, today = _pnl(since_start["total"], 0.0), _pnl(today["total"], 0.0)
            t = traders.setdefault(trader, {"positions": {}, **_pnl(0.0, 0.0), "today": _pnl(0.0, 0.0)})
            _add(t, since_start)
            _add(t["today"], today)
            if h.since_start.qty and settled is None:
                desk_positions[symbol] = desk_positions.get(symbol, 0) + h.since_start.qty
            closed = h.since_start.qty == 0 and h.sod_qty == 0 and not h.today
            if closed or (settled is not None and self.trading_day.day_of(settled.time) < self.today):
                continue  # closed out or settled before today: counted in the totals, not listed
            t["positions"][symbol] = {
                "qty": h.since_start.qty,
                "avg_px": round(h.since_start.avg_px, 4),
                "mark": price,
                "mark_source": mark.source if mark else "none",
                "mark_stale": mark.stale if mark else True,
                "mark_age_s": round(now - mark.time, 1) if mark else None,
                "expired": self.expired(symbol, now),
                "settled": settled is not None,
                "sod_qty": h.sod_qty,
                "sod_mark": sod.price if sod else None,
                "sod_mark_late": bool(h.sod_qty) and (sod is None or sod.late),
                "screen_qty": h.screen,
                "today": _rounded(today),
                **_rounded(since_start),
            }
        for t in traders.values():
            _add(desk, t)
            _add(desk_today, t["today"])
            t.update(_rounded({k: t[k] for k in ("realized", "unrealized", "total")}))
            t["today"] = _rounded(t["today"])
        for trader, t in traders.items():
            t["limits"] = {"loss": self._loss_usage(trader, t), "position": self._position_usage(trader, t)}
        waiting = sum(len(f) for f in self._waiting.values())
        return {
            "time": datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds"),
            "trading_day": self.trading_day.describe(self.today),
            "traders": traders,
            "desk": {**_rounded(desk), "today": _rounded(desk_today), "positions": desk_positions},
            "unattributed_fills": waiting,
        }

    def _loss_usage(self, trader: str, t: Mapping[str, Any]) -> dict[str, Any] | None:
        """Today's loss against the trader's max loss for the day."""
        limit = self.rules.limits_for(trader).max_loss if self.rules else None
        if not limit:
            return None
        loss = max(0.0, -t["today"]["total"])
        return {"used": round(loss, 2), "limit": limit, "pct": _pct(loss, limit)}

    def _position_usage(self, trader: str, t: Mapping[str, Any]) -> dict[str, Any] | None:
        """The contract closest to its own position limit (limits can differ by market and product)."""
        worst = None
        for symbol, p in t["positions"].items():
            limit = self.rules.limits_for(trader, symbol).max_position if self.rules else None
            if limit:
                used = 0 if p["settled"] else abs(p["qty"])  # a settled position is closed
                usage = {"used": used, "limit": limit, "pct": _pct(used, limit), "symbol": symbol}
                if worst is None or usage["pct"] > worst["pct"]:
                    worst = usage
        return worst

    def breaches(self, snapshot: Mapping[str, Any]) -> dict[str, str]:
        """trader -> reason, for every trader whose loss today has reached their limit."""
        out = {}
        for trader, t in snapshot["traders"].items():
            loss = t["limits"]["loss"]
            if loss and loss["used"] >= loss["limit"]:
                out[trader] = f"today's loss {loss['used']:.2f} reached the daily limit of {loss['limit']:.2f}"
        return out
