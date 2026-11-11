"""Orderentry's orders and positions across restarts.

Two sources, used together:
  * the state file (var/oe/orders.json), written every second and at shutdown: live orders, orders
    closed in the last week, each trader's positions, and the id of the last `orders` stream entry
    the file already includes
  * the `orders` stream after that id: updates from the last moments before a crash

Every order carries a version that goes up on each update, so an update read twice, or an older one
read after a newer one, changes nothing. Positions move only by the change in an order's filled
quantity (less what the exchange has busted), so they cannot be counted twice either.

What this cannot see is what happened at the exchange while orderentry was down. Within the FIX
week the exchange resends those ExecutionReports after logon. Across the weekly sequence reset
(Sunday 06:15 UTC) it cannot, so orders still live from an earlier week are cancelled (see
OrderEntryApp.cancel_unconfirmed).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from trading_fix import fix_week_start

from .orders import Order
from .sink import STREAM

log = logging.getLogger("oe.recovery")

KEEP_CLOSED_S = 7 * 24 * 3600  # closed orders stay in the file (and the blotter) this long


@dataclass
class Recovered:
    orders: dict[str, Order] = field(default_factory=dict)  # ClOrdID -> order
    positions: dict[tuple[str, str], int] = field(default_factory=dict)  # (trader, symbol) -> lots
    stream_id: str | None = None  # last `orders` entry included
    last_seen: float = 0.0  # time.time() of the newest thing recorded
    replayed: int = 0  # stream entries applied on top of the file

    def from_earlier_week(self, now: datetime | None = None) -> bool:
        """True if orderentry last recorded anything before the most recent weekly reset."""
        return bool(self.last_seen) and self.last_seen < fix_week_start(now).timestamp()


def apply(rec: Recovered, update: Mapping[str, Any]) -> bool:
    """Apply one order update; False if we already have this version or a newer one."""
    new = Order.from_dict(dict(update))
    old = rec.orders.get(new.cl_ord_id)
    if old is not None and new.version < old.version:
        return False
    filled = new.net_filled - (old.net_filled if old else 0)
    if filled:
        key = (new.trader, new.symbol)
        rec.positions[key] = rec.positions.get(key, 0) + (filled if new.side == "BUY" else -filled)
    rec.orders[new.cl_ord_id] = new
    return True


def _age_s(order: Order, now: float) -> float:
    try:
        return now - datetime.fromisoformat(order.updated).timestamp()
    except ValueError:
        return 0.0


def save_state(
    path: Path,
    orders: Iterable[Order],
    positions: Mapping[tuple[str, str], int],
    stream_id: str | None,
    now: float | None = None,
) -> None:
    now = time.time() if now is None else now
    data = {
        "saved": now,
        "stream_id": stream_id,
        "orders": [o.to_dict() for o in orders if o.live or _age_s(o, now) < KEEP_CLOSED_S],
        "positions": [[trader, symbol, qty] for (trader, symbol), qty in positions.items() if qty],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash leaves the old file or the new one, never half
    except OSError as exc:
        log.error("could not save orders to %s: %s", path, exc)


def load_state(path: Path) -> Recovered:
    """What the file holds, or nothing (first run, or after a clean slate)."""
    if not path.exists():
        return Recovered()
    data = json.loads(path.read_text(encoding="utf-8"))
    orders = [Order.from_dict(d) for d in data.get("orders", [])]
    return Recovered(
        orders={o.cl_ord_id: o for o in orders},
        positions={(t, s): int(q) for t, s, q in data.get("positions", [])},
        stream_id=data.get("stream_id"),
        last_seen=float(data.get("saved", 0.0)),
    )


async def recover(redis: Redis, path: Path, page: int = 1000) -> Recovered:
    """The state file plus every `orders` entry after the one it includes. With no file, the whole
    stream is replayed (the first run after upgrading, or after `rm -rf var`)."""
    rec = load_state(path)
    start = f"({rec.stream_id}" if rec.stream_id else "-"  # "(" = exclusive
    while True:
        entries = await redis.xrange(STREAM, min=start, max="+", count=page)
        if not entries:
            break
        for entry_id, fields in entries:
            eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
            raw = fields[b"data"] if b"data" in fields else fields["data"]
            if apply(rec, json.loads(raw)):
                rec.replayed += 1
            rec.stream_id = eid
            rec.last_seen = max(rec.last_seen, int(eid.split("-")[0]) / 1000)
        start = f"({rec.stream_id}"
    return rec
