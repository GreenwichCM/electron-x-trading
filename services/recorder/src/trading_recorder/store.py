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
    "message, created_at, updated_at, source, exchange_user"
)
# An order from the ElectronX screen has no trader: it belongs to whoever has its ElectronX id, if only one does.
TRADER = (
    "COALESCE(NULLIF(%(trader)s, N''), (SELECT CASE WHEN COUNT(*) = 1 THEN MIN(trader_id) END "
    "FROM electronx.traders WHERE exchange_trader_id = %(exchange_user)s), N'')"
)
ORDER_VALUES = (
    f"%(cl_ord_id)s, %(version)s, {TRADER}, %(symbol)s, %(side)s, %(qty)s, %(price)s, %(tif)s, %(status)s, "
    "%(order_id)s, %(cum_qty)s, %(avg_px)s, %(message)s, %(created_at)s, %(updated_at)s, %(source)s, "
    "%(exchange_user)s"
)

INSERT_ORDER_EVENT = f"""
IF NOT EXISTS (SELECT 1 FROM electronx.order_events WHERE stream_id = %(stream_id)s)
    INSERT electronx.order_events (stream_id, {ORDER_COLUMNS}) VALUES (%(stream_id)s, {ORDER_VALUES});
"""

# The latest state wins by version; an equal version overwrites (updates from before step 10 are all 0).
UPSERT_ORDER = f"""
UPDATE electronx.orders SET
    version = %(version)s, status = %(status)s, order_id = %(order_id)s, cum_qty = %(cum_qty)s,
    avg_px = %(avg_px)s, message = %(message)s, updated_at = %(updated_at)s, last_stream_id = %(stream_id)s
WHERE cl_ord_id = %(cl_ord_id)s AND version <= %(version)s;
IF @@ROWCOUNT = 0 AND NOT EXISTS (SELECT 1 FROM electronx.orders WHERE cl_ord_id = %(cl_ord_id)s)
    INSERT electronx.orders ({ORDER_COLUMNS}, last_stream_id) VALUES ({ORDER_VALUES}, %(stream_id)s);
"""

INSERT_FILL = """
IF NOT EXISTS (SELECT 1 FROM electronx.fills WHERE exec_id = %(exec_id)s)
    INSERT electronx.fills (exec_id, stream_id, order_id, cl_ord_id, account, symbol, side, qty, price, exec_time,
                            exchange_user, manual)
    VALUES (%(exec_id)s, %(stream_id)s, %(order_id)s, %(cl_ord_id)s, %(account)s, %(symbol)s, %(side)s,
            %(qty)s, %(price)s, %(exec_time)s, %(exchange_user)s, %(manual)s);
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
    cur = conn.cursor()
    cur.execute(INSERT_ORDER_EVENT, row)
    cur.execute(UPSERT_ORDER, row)


def write_fill(conn: Connection, row: dict[str, Any]) -> None:
    conn.cursor().execute(INSERT_FILL, row)


def write_alert(conn: Connection, row: dict[str, Any]) -> None:
    conn.cursor().execute(INSERT_ALERT, row)


def save_offset(conn: Connection, stream: str, last_id: str) -> None:
    conn.cursor().execute(SAVE_OFFSET, {"stream": stream, "last_id": last_id})


def load_offsets(conn: Connection) -> dict[str, str]:
    cur = conn.cursor()
    cur.execute("SELECT stream, last_id FROM electronx.stream_offsets")
    return {stream: last_id for stream, last_id in cur.fetchall()}
