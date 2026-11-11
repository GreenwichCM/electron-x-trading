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


def _opt_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _opt_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def order_row(stream_id: str, o: dict[str, Any]) -> dict[str, Any]:
    """An update to one of our orders (stream `orders`) or to one from the ElectronX screen
    (`screen_orders`, from dropcopy: source "electronx", no trader, the trader's ElectronX id instead):
    the order's state after it, and the event itself (what happened, who or what caused it, the
    exchange's report). Updates from before step 20 carry no event fields: those columns stay NULL."""
    leaves = o.get("leaves_qty", o.get("leaves"))
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
        # the event
        "event": o.get("event") or None,
        "by_user": (o.get("by") or "")[:128] or None,
        "reason": (o.get("reason") or "")[:400] or None,
        "event_at": parse_time(o.get("event_time")),
        "request_id": o.get("request_id") or None,
        "exec_id": o.get("exec_id") or None,
        "exec_type": o.get("exec_type") or None,
        "exchange_time": parse_time(o.get("exchange_time")),
        "last_qty": _opt_int(o.get("last_qty")),
        "last_px": _opt_float(o.get("last_px")),
        "leaves_qty": _opt_int(leaves),
        "reject_code": _opt_int(o.get("reject_code")),
        "restatement_reason": _opt_int(o.get("restatement_reason")),
        "trade_id": o.get("trade_id") or None,
        "aggressor": o.get("aggressor"),
        "commission": _opt_float(o.get("commission")),
        "account_type": o.get("account_type") or None,
        # as sent to the exchange
        "account": o.get("account") or None,
        "capacity": o.get("capacity") or None,
        "ord_type": o.get("ord_type") or None,
        "manual": o.get("manual"),
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
        "trade_id": f.get("trade_id") or None,  # these four: fills from before step 20 have none
        "aggressor": f.get("aggressor"),
        "commission": _opt_float(f.get("commission")),
        "leaves_qty": _opt_int(f.get("leaves_qty")),
        "capacity": f.get("capacity") or None,
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
