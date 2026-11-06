"""Risk per trader and for the desk, from the exchange's fills and live marks.

Fills come from dropcopy (the exchange's record) and carry a ClOrdID but no trader; the trader is
found from orderentry's `orders` stream. A fill whose order is not known yet waits until it is.

Limits come from the trading rules in SQL Server (set_rules): each trader's max loss, and the max
position of each contract. A trader with no loss limit set shows no usage and is never blocked here;
orderentry rejects their orders anyway, since its rules fail closed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from trading_core import TradingRules

from .marks import Mark, choose_mark
from .positions import Position


def _pct(used: float, limit: float) -> float:
    return round(100.0 * used / limit, 1) if limit else 0.0


class RiskEngine:
    def __init__(
        self,
        rules: TradingRules | None = None,
        max_spread: float = 5.0,  # USD/MWh; wider books are not marked at their mid
        clock: Callable[[], float] = time.time,
    ):
        self.rules = rules
        self.max_spread = max_spread
        self.clock = clock
        self.traders: dict[str, str] = {}  # ClOrdID -> trader
        self.positions: dict[tuple[str, str], Position] = {}  # (trader, symbol) -> position
        self.books: dict[str, dict[str, Any]] = {}
        self.instruments: dict[str, dict[str, Any]] = {}
        self.marks: dict[str, Mark] = {}
        self._waiting: dict[str, list[dict[str, Any]]] = {}  # ClOrdID -> fills whose order is not known yet
        self._seen: set[str] = set()  # ExecIDs already applied

    # ---- inputs ----------------------------------------------------------------------------

    def on_order(self, order: Mapping[str, Any]) -> None:
        cl = order["cl_ord_id"]
        self.traders[cl] = order["trader"]
        for fill in self._waiting.pop(cl, []):
            self._apply(order["trader"], fill)

    def on_fill(self, fill: Mapping[str, Any]) -> None:
        if fill["exec_id"] in self._seen:
            return
        self._seen.add(fill["exec_id"])
        trader = self.traders.get(fill["cl_ord_id"])
        if trader is None:
            self._waiting.setdefault(fill["cl_ord_id"], []).append(dict(fill))
        else:
            self._apply(trader, fill)

    def on_book(self, book: Mapping[str, Any]) -> None:
        self.books[book["symbol"]] = dict(book)
        self._remark(book["symbol"])

    def set_rules(self, rules: TradingRules) -> None:
        self.rules = rules

    def on_instrument(self, instrument: Mapping[str, Any]) -> None:
        self.instruments[instrument["symbol"]] = dict(instrument)

    def _apply(self, trader: str, fill: Mapping[str, Any]) -> None:
        key = (trader, fill["symbol"])
        self.positions.setdefault(key, Position()).apply(fill["side"], int(fill["qty"]), float(fill["price"]))
        if fill["symbol"] not in self.marks:
            self._remark(fill["symbol"])

    def _remark(self, symbol: str) -> None:
        self.marks[symbol] = choose_mark(
            self.books.get(symbol),
            self.instruments.get(symbol),
            self.marks.get(symbol),
            self.clock(),
            self.max_spread,
        )

    # ---- outputs ---------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        traders: dict[str, Any] = {}
        desk_positions: dict[str, int] = {}
        desk = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
        for (trader, symbol), pos in sorted(self.positions.items()):
            mark = self.marks.get(symbol)
            unrealized = pos.unrealized(mark.price if mark else None)
            t = traders.setdefault(trader, {"positions": {}, "realized": 0.0, "unrealized": 0.0, "total": 0.0})
            t["positions"][symbol] = {
                "qty": pos.qty,
                "avg_px": round(pos.avg_px, 4),
                "mark": mark.price if mark else None,
                "mark_source": mark.source if mark else "none",
                "mark_stale": mark.stale if mark else True,
                "mark_age_s": round(now - mark.time, 1) if mark else None,
                "realized": round(pos.realized, 2),
                "unrealized": round(unrealized, 2),
            }
            t["realized"] += pos.realized
            t["unrealized"] += unrealized
            desk_positions[symbol] = desk_positions.get(symbol, 0) + pos.qty
        for t in traders.values():
            t["total"] = t["realized"] + t["unrealized"]
            for k in ("realized", "unrealized", "total"):
                desk[k] += t[k]
                t[k] = round(t[k], 2)
        for trader, t in traders.items():
            t["limits"] = {"loss": self._loss_usage(trader, t), "position": self._position_usage(trader, t)}
        waiting = sum(len(f) for f in self._waiting.values())
        return {
            "time": datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds"),
            "traders": traders,
            "desk": {**{k: round(v, 2) for k, v in desk.items()}, "positions": desk_positions},
            "unattributed_fills": waiting,
        }

    def _loss_usage(self, trader: str, t: Mapping[str, Any]) -> dict[str, Any] | None:
        limit = self.rules.limits_for(trader).max_loss if self.rules else None
        if not limit:
            return None
        loss = max(0.0, -t["total"])
        return {"used": round(loss, 2), "limit": limit, "pct": _pct(loss, limit)}

    def _position_usage(self, trader: str, t: Mapping[str, Any]) -> dict[str, Any] | None:
        """The contract closest to its own position limit (limits can differ by market and product)."""
        worst = None
        for symbol, p in t["positions"].items():
            limit = self.rules.limits_for(trader, symbol).max_position if self.rules else None
            if limit:
                usage = {"used": abs(p["qty"]), "limit": limit, "pct": _pct(abs(p["qty"]), limit), "symbol": symbol}
                if worst is None or usage["pct"] > worst["pct"]:
                    worst = usage
        return worst

    def breaches(self, snapshot: Mapping[str, Any]) -> dict[str, str]:
        """trader -> reason, for every trader whose loss has reached their limit."""
        out = {}
        for trader, t in snapshot["traders"].items():
            loss = t["limits"]["loss"]
            if loss and loss["used"] >= loss["limit"]:
                out[trader] = f"loss {loss['used']:.2f} reached the limit of {loss['limit']:.2f}"
        return out
