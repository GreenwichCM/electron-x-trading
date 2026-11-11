"""Compare the two records of our orders' ExecutionReports: orderentry's session (electronx.order_events)
and the drop copy (electronx.exchange_reports).

The exchange sends every report about our orders on both sessions. A report found in one and not in the
other means a session missed it, or a service did not record it. Reports are matched by ExecID, or else
by the same order, ExecType and filled quantity (in case the two sessions number their reports
differently). The most recent `settle_s` are left out: the recorder writes in batches.

    uv run --env-file services/db.env python -m trading_db reconcile [--hours 24]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .connection import Connection
from .migrate import SCHEMA

SETTLE_S = 60

OURS = f"""
EXISTS (SELECT 1 FROM {SCHEMA}.orders AS o WHERE o.source = 'app'
        AND (o.order_id = r.order_id OR o.cl_ord_id = r.cl_ord_id OR o.cl_ord_id = r.orig_cl_ord_id))
"""
SAME_REPORT = (
    "(r.exec_id = e.exec_id OR (r.order_id = e.order_id AND r.exec_type = e.exec_type AND r.cum_qty = e.cum_qty))"
)

DROP_COPY_ONLY = f"""
SELECT TOP (%(limit)s) r.cl_ord_id, r.order_id, r.exec_id, r.exec_type, r.ord_status, r.cum_qty, r.received_at
FROM {SCHEMA}.exchange_reports AS r
WHERE r.received_at >= %(since)s AND r.received_at < %(until)s AND {OURS}
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.order_events AS e WHERE {SAME_REPORT})
ORDER BY r.received_at
"""

ORDERENTRY_ONLY = f"""
SELECT TOP (%(limit)s) e.cl_ord_id, e.order_id, e.exec_id, e.exec_type, e.status, e.cum_qty, e.event_at
FROM {SCHEMA}.order_events AS e
WHERE e.source = 'app' AND e.event = 'exchange_report' AND e.event_at >= %(since)s AND e.event_at < %(until)s
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.exchange_reports AS r WHERE {SAME_REPORT})
ORDER BY e.event_at
"""

COUNTS = f"""
SELECT
  (SELECT COUNT(*) FROM {SCHEMA}.exchange_reports AS r
   WHERE r.received_at >= %(since)s AND r.received_at < %(until)s AND {OURS}),
  (SELECT COUNT(*) FROM {SCHEMA}.order_events AS e
   WHERE e.source = 'app' AND e.event = 'exchange_report' AND e.event_at >= %(since)s AND e.event_at < %(until)s)
"""


@dataclass
class Reconciliation:
    since: datetime
    until: datetime
    drop_copy_reports: int = 0  # reports about our orders on the drop copy
    orderentry_reports: int = 0  # reports orderentry's session received
    drop_copy_only: list[tuple[Any, ...]] = field(default_factory=list)  # orderentry never recorded these
    orderentry_only: list[tuple[Any, ...]] = field(default_factory=list)  # the drop copy never recorded these

    @property
    def ok(self) -> bool:
        return not self.drop_copy_only and not self.orderentry_only


def reconcile(
    conn: Connection,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
) -> Reconciliation:
    """Reports between `since` and `until` (naive UTC; by default the last day, up to SETTLE_S ago)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    until = until or now - timedelta(seconds=SETTLE_S)
    since = since or until - timedelta(days=1)
    params = {"since": since, "until": until, "limit": limit}
    out = Reconciliation(since, until)
    cur = conn.cursor()
    cur.execute(COUNTS, params)
    out.drop_copy_reports, out.orderentry_reports = cur.fetchone()
    cur.execute(DROP_COPY_ONLY, params)
    out.drop_copy_only = list(cur.fetchall())
    cur.execute(ORDERENTRY_ONLY, params)
    out.orderentry_only = list(cur.fetchall())
    return out
