"""Choosing the price each position is valued at (the "mark").

For a live contract, in order, the first that applies (expired and settled contracts are handled in
engine.py):
  1. mid of a two-sided book whose spread is no wider than `max_spread`
  2. the last trade, if it lies inside the current bid and ask
  3. for a Bounded Future, the exchange's day-ahead price (from the Snapshot API), kept inside the
     current bid and ask. If the book is stale (market data is down) the previous mark comes first.
     A BOPT's day-ahead price is its strike, not a price for the option, so it is never used.
  4. the previous mark, flagged stale
Every mark is clamped to the contract's price limits and carries its source and the time it was set,
so the dashboard can show "mid, 2 s ago" or "stale, 14 min".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Mark:
    price: float | None
    source: str  # "mid", "last trade", "day-ahead" or "none"
    time: float  # when it was set (time.time())
    stale: bool = False


def day_ahead(book: dict[str, Any] | None) -> float | None:
    """The exchange's day-ahead price for a Bounded Future, if marketdata has one."""
    if book is None or not str(book.get("symbol", "")).startswith("BFUT"):
        return None
    price = ((book.get("marks") or {}).get("dam") or {}).get("price")
    return float(price) if price is not None else None


def choose_mark(
    book: dict[str, Any] | None,
    instrument: dict[str, Any] | None,
    previous: Mark | None,
    now: float,
    max_spread: float,
) -> Mark:
    price: float | None = None
    source = "none"
    live = book is not None and not book.get("stale")
    bid = ask = None
    if live:
        bid, ask = book.get("bid"), book.get("ask")
        last = (book.get("last_trade") or {}).get("price")
        if bid is not None and ask is not None and ask - bid <= max_spread:
            price, source = (bid + ask) / 2, "mid"
        elif last is not None and (bid is None or last >= bid) and (ask is None or last <= ask):
            price, source = last, "last trade"
    had_mark = previous is not None and previous.price is not None
    dam = day_ahead(book)
    if price is None and dam is not None and (live or not had_mark):
        price, source = dam, "day-ahead"
        if bid is not None:
            price = max(price, bid)
        if ask is not None:
            price = min(price, ask)
    if price is None:
        if had_mark:
            return Mark(previous.price, previous.source, previous.time, stale=True)
        return Mark(None, "none", now, stale=True)
    if instrument is not None:
        low, high = instrument.get("low_limit"), instrument.get("high_limit")
        if low is not None:
            price = max(price, low)
        if high is not None:
            price = min(price, high)
    return Mark(price, source, now)
