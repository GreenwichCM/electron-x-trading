"""Stream entries as table rows (no database code here, so it is easy to test)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def parse_time(value: str | None) -> datetime | None:
    """ISO 8601 (our services) or FIX UTCTimestamp (20261106-02:56:02.069, up to nanoseconds), as naive
    UTC for datetime2."""
    if not value:
        return None
    whole, dot, fraction = value.partition(".")
    fix_value = f"{whole}.{fraction[:6]}" if dot else whole  # Python reads at most microseconds
    for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(fix_value, fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def order_row(stream_id: str, o: dict[str, Any]) -> dict[str, Any]:
    """An update to one of our orders (stream `orders`) or to one from the ElectronX screen
    (`screen_orders`, from dropcopy: source "electronx", no trader, the trader's ElectronX id instead)."""
    return {
        "stream_id": stream_id,
        "cl_ord_id": o["cl_ord_id"],
        "version": int(o.get("version", 0)),  # updates from before step 10 have none
        "trader": o["trader"],
        "symbol": o["symbol"],
        "side": o["side"],
        "qty": int(o["qty"]),
        "price": float(o["price"]),
        "tif": o.get("tif", ""),
        "status": o["status"],
        "order_id": o.get("order_id", ""),
        "cum_qty": int(o.get("cum_qty", 0)),
        "avg_px": float(o.get("avg_px", 0.0)),
        "message": (o.get("text") or "")[:400],
        "created_at": parse_time(o.get("created")),
        "updated_at": parse_time(o.get("updated")),
        "source": o.get("source") or "app",
        "exchange_user": o.get("exchange_user") or None,
    }


def fill_row(stream_id: str, f: dict[str, Any]) -> dict[str, Any]:
    return {
        "exec_id": f["exec_id"],
        "stream_id": stream_id,
        "order_id": f.get("order_id", ""),
        "cl_ord_id": f.get("cl_ord_id", ""),
        "account": f.get("account", ""),
        "symbol": f["symbol"],
        "side": f["side"],
        "qty": int(f["qty"]),
        "price": float(f["price"]),
        "exec_time": parse_time(f.get("time")),
        "exchange_user": f.get("user") or None,  # TargetSubID(57); fills from before step 13e have none
        "manual": f.get("manual"),
    }


def alert_row(stream_id: str, a: dict[str, Any], raw: str) -> dict[str, Any]:
    """Kill switch states carry no type (they are the switch's state); trader blocks do."""
    kind = a.get("type") or ("killswitch_on" if a.get("active") else "killswitch_off")
    return {
        "stream_id": stream_id,
        "type": kind,
        "trader": a.get("trader"),
        "by_user": a.get("by", ""),
        "reason": (a.get("reason") or "")[:400],
        "alert_time": parse_time(a.get("time")),
        "data": raw,
    }


def decode_entry(fields: dict) -> tuple[dict[str, Any], str]:
    """The `data` field of a stream entry: (parsed, raw JSON)."""
    raw = fields[b"data"] if b"data" in fields else fields["data"]
    raw = raw.decode() if isinstance(raw, bytes) else raw
    return json.loads(raw), raw
