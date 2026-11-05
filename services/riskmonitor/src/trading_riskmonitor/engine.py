"""Risk per trader and for the desk, from the exchange's fills and live marks.

Fills come from dropcopy (the exchange's record) and carry a ClOrdID but no trader; the trader is
found from orderentry's `orders` stream. A fill whose order is not known yet waits until it is.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .marks import Mark, choose_mark
from .positions import Position


@dataclass(frozen=True)
class Limits:
    max_loss: float = 5_000.0  # USD, per trader; reaching it blocks the trader
    max_position: int = 50  # lots, per trader and contract; shown as usage, enforced by orderentry
    max_spread: float = 5.0  # USD/MWh; wider books are not marked at their mid


def _pct(used: float, limit: float) -> float:
    return round(100.0 * used / limit, 1) if limit else 0.0


class RiskEngine:
    def __init__(self, limits: Limits | None = None, clock: Callable[[], float] = time.time):
        self.limits = limits or Limits()
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
            self.limits.max_spread,
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
            loss = max(0.0, -t["total"])
            biggest = max((abs(p["qty"]) for p in t["positions"].values()), default=0)
            max_loss, max_pos = self.limits.max_loss, self.limits.max_position
            t["limits"] = {
                "loss": {"used": round(loss, 2), "limit": max_loss, "pct": _pct(loss, max_loss)},
                "position": {"used": biggest, "limit": max_pos, "pct": _pct(biggest, max_pos)},
            }
        waiting = sum(len(f) for f in self._waiting.values())
        return {
            "time": datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds"),
            "traders": traders,
            "desk": {**{k: round(v, 2) for k, v in desk.items()}, "positions": desk_positions},
            "unattributed_fills": waiting,
        }

    def breaches(self, snapshot: Mapping[str, Any]) -> dict[str, str]:
        """trader -> reason, for every trader whose loss has reached the limit."""
        out = {}
        for trader, t in snapshot["traders"].items():
            loss = t["limits"]["loss"]
            if loss["limit"] and loss["used"] >= loss["limit"]:
                out[trader] = f"loss {loss['used']:.2f} reached the limit of {loss['limit']:.2f}"
        return out
