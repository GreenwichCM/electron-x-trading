"""Choosing the price each position is valued at (the "mark").

In order, the first that applies (settlement and snapshot marks join this list later):
  1. mid of a two-sided book whose spread is no wider than `max_spread`
  2. the last trade, if it lies inside the current bid and ask
  3. the previous mark, flagged stale
Every mark is clamped to the contract's price limits and carries its source and the time it was set,
so the dashboard can show "mid, 2 s ago" or "stale, 14 min".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Mark:
    price: float | None
    source: str  # "mid", "last trade" or "none"
    time: float  # when it was set (time.time())
    stale: bool = False


def choose_mark(
    book: dict[str, Any] | None,
    instrument: dict[str, Any] | None,
    previous: Mark | None,
    now: float,
    max_spread: float,
) -> Mark:
    price: float | None = None
    source = "none"
    if book is not None and not book.get("stale"):
        bid, ask = book.get("bid"), book.get("ask")
        last = (book.get("last_trade") or {}).get("price")
        if bid is not None and ask is not None and ask - bid <= max_spread:
            price, source = (bid + ask) / 2, "mid"
        elif last is not None and (bid is None or last >= bid) and (ask is None or last <= ask):
            price, source = last, "last trade"
    if price is None:
        if previous is not None and previous.price is not None:
            return Mark(previous.price, previous.source, previous.time, stale=True)
        return Mark(None, "none", now, stale=True)
    if instrument is not None:
        low, high = instrument.get("low_limit"), instrument.get("high_limit")
        if low is not None:
            price = max(price, low)
        if high is not None:
            price = min(price, high)
    return Mark(price, source, now)
