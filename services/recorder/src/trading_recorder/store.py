"""Writing rows to SQL Server. The caller commits: a batch of rows and the new stream offsets go in
one transaction, so after a crash the recorder carries on from exactly where its last commit ended.

Every insert also checks for the row first, so reading the same entry twice (say, the offsets were
reset) never duplicates anything.
"""

from __future__ import annotations

from typing import Any

from trading_db import Connection

ORDER_COLUMNS = (
    "cl_ord_id, version, trader, symbol, side, qty, price, tif, status, order_id, cum_qty, avg_px, "
    "message, created_at, updated_at, source, exchange_user, busted_qty"
)
# An order from the ElectronX screen has no trader: it belongs to whoever has its ElectronX id, if only one does.
TRADER = (
    "COALESCE(NULLIF(%(trader)s, N''), (SELECT CASE WHEN COUNT(*) = 1 THEN MIN(trader_id) END "
    "FROM electronx.traders WHERE exchange_trader_id = %(exchange_user)s), N'')"
)
ORDER_VALUES = (
    f"%(cl_ord_id)s, %(version)s, {TRADER}, %(symbol)s, %(side)s, %(qty)s, %(price)s, %(tif)s, %(status)s, "
    "%(order_id)s, %(cum_qty)s, %(avg_px)s, %(message)s, %(created_at)s, %(updated_at)s, %(source)s, "
    "%(exchange_user)s, %(busted_qty)s"
)

# The event itself: what happened, who or what caused it, and the exchange's report (order_events only).
EVENT_COLUMNS = (
    "event, by_user, reason, event_at, request_id, exec_id, exec_type, exec_ref_id, exchange_time, last_qty, "
    "last_px, "
    "leaves_qty, reject_code, restatement_reason, trade_id, aggressor, commission, account_type, account, capacity, "
    "ord_type, manual"
)
EVENT_VALUES = ", ".join(f"%({c.strip()})s" for c in EVENT_COLUMNS.split(","))
# What the latest state also keeps (orders only).
STATE_COLUMNS = "account, capacity, ord_type, manual, leaves_qty, last_event"
STATE_VALUES = "%(account)s, %(capacity)s, %(ord_type)s, %(manual)s, %(leaves_qty)s, %(event)s"

INSERT_ORDER_EVENT = f"""
IF NOT EXISTS (SELECT 1 FROM electronx.order_events WHERE stream_id = %(stream_id)s)
    INSERT electronx.order_events (stream_id, {ORDER_COLUMNS}, {EVENT_COLUMNS})
    VALUES (%(stream_id)s, {ORDER_VALUES}, {EVENT_VALUES});
"""

# The latest state wins by version; an equal version overwrites (updates from before step 10 are all 0).
UPSERT_ORDER = f"""
UPDATE electronx.orders SET
    version = %(version)s, status = %(status)s, order_id = %(order_id)s, cum_qty = %(cum_qty)s,
    avg_px = %(avg_px)s, message = %(message)s, updated_at = %(updated_at)s, last_stream_id = %(stream_id)s,
    leaves_qty = %(leaves_qty)s, last_event = %(event)s, busted_qty = COALESCE(%(busted_qty)s, busted_qty),
    account = COALESCE(%(account)s, account), capacity = COALESCE(%(capacity)s, capacity),
    ord_type = COALESCE(%(ord_type)s, ord_type), manual = COALESCE(%(manual)s, manual),
    exchange_user = COALESCE(%(exchange_user)s, exchange_user)
WHERE cl_ord_id = %(cl_ord_id)s AND version <= %(version)s;
IF @@ROWCOUNT = 0 AND NOT EXISTS (SELECT 1 FROM electronx.orders WHERE cl_ord_id = %(cl_ord_id)s)
    INSERT electronx.orders ({ORDER_COLUMNS}, {STATE_COLUMNS}, last_stream_id)
    VALUES ({ORDER_VALUES}, {STATE_VALUES}, %(stream_id)s);
"""

INSERT_FILL = """
IF NOT EXISTS (SELECT 1 FROM electronx.fills WHERE exec_id = %(exec_id)s)
    INSERT electronx.fills (exec_id, stream_id, order_id, cl_ord_id, account, symbol, side, qty, price, exec_time,
                            exchange_user, manual, trade_id, aggressor, commission, leaves_qty, capacity)
    VALUES (%(exec_id)s, %(stream_id)s, %(order_id)s, %(cl_ord_id)s, %(account)s, %(symbol)s, %(side)s,
            %(qty)s, %(price)s, %(exec_time)s, %(exchange_user)s, %(manual)s, %(trade_id)s, %(aggressor)s,
            %(commission)s, %(leaves_qty)s, %(capacity)s);
"""

REPORT_COLUMNS = (
    "exec_id, stream_id, received_at, exchange_time, order_id, cl_ord_id, orig_cl_ord_id, exchange_user, account, "
    "account_type, capacity, manual, symbol, side, exec_type, exec_ref_id, ord_status, order_qty, price, tif, "
    "cum_qty, leaves_qty, avg_px, last_qty, last_px, trade_id, aggressor, commission, reject_code, "
    "restatement_reason, text, raw"
)
INSERT_REPORT = f"""
IF NOT EXISTS (SELECT 1 FROM electronx.exchange_reports WHERE exec_id = %(exec_id)s)
    INSERT electronx.exchange_reports ({REPORT_COLUMNS})
    VALUES ({", ".join(f"%({c.strip()})s" for c in REPORT_COLUMNS.split(","))});
"""

# A bust keeps the fill it cancels, marked: sum `qty` over fills WHERE busted_at IS NULL for what stands.
MARK_BUST = """
UPDATE electronx.fills SET busted_at = %(busted_at)s, bust_exec_id = %(bust_exec_id)s, bust_reason = %(bust_reason)s
WHERE exec_id = %(exec_ref_id)s AND bust_exec_id IS NULL;
"""

INSERT_ALERT = """
IF NOT EXISTS (SELECT 1 FROM electronx.alerts WHERE stream_id = %(stream_id)s)
    INSERT electronx.alerts (stream_id, type, trader, by_user, reason, alert_time, data)
    VALUES (%(stream_id)s, %(type)s, %(trader)s, %(by_user)s, %(reason)s, %(alert_time)s, %(data)s);
"""

SAVE_OFFSET = """
UPDATE electronx.stream_offsets SET last_id = %(last_id)s, updated_at = sysutcdatetime() WHERE stream = %(stream)s;
IF @@ROWCOUNT = 0 INSERT electronx.stream_offsets (stream, last_id) VALUES (%(stream)s, %(last_id)s);
"""


def write_order(conn: Connection, row: dict[str, Any]) -> None:
    row = _precise(row, "exchange_time")
    cur = conn.cursor()
    cur.execute(INSERT_ORDER_EVENT, row)
    cur.execute(UPSERT_ORDER, row)


def _precise(row: dict[str, Any], key: str) -> dict[str, Any]:
    """A datetime as text, so it keeps its microseconds: the driver would send it to the millisecond only."""
    value = row.get(key)
    return row if value is None else {**row, key: value.isoformat(timespec="microseconds")}


def write_report(conn: Connection, row: dict[str, Any]) -> None:
    conn.cursor().execute(INSERT_REPORT, _precise(row, "exchange_time"))


def write_fill(conn: Connection, row: dict[str, Any]) -> None:
    conn.cursor().execute(INSERT_FILL, row)


def write_bust(conn: Connection, row: dict[str, Any]) -> int:
    """Mark the busted fill; returns 1, or 0 if that fill is not recorded (or was marked already)."""
    cur = conn.cursor()
    cur.execute(MARK_BUST, _precise(row, "busted_at"))
    return cur.rowcount


def write_alert(conn: Connection, row: dict[str, Any]) -> None:
    conn.cursor().execute(INSERT_ALERT, row)


def save_offset(conn: Connection, stream: str, last_id: str) -> None:
    conn.cursor().execute(SAVE_OFFSET, {"stream": stream, "last_id": last_id})


def load_offsets(conn: Connection) -> dict[str, str]:
    cur = conn.cursor()
    cur.execute("SELECT stream, last_id FROM electronx.stream_offsets")
    return {stream: last_id for stream, last_id in cur.fetchall()}
