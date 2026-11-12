"""Whose orders from the ElectronX screen are whose.

An order from the screen carries the trader's ElectronX login (TargetSubID 57, `exchange_user`), which is
their user id here in lower case. The recorder fills in `trader` as it records each update, if that user
exists; rows recorded before they were added are left blank. fill_in_traders gives those rows to the
user: approving a new user on the Trading rules page does it for them, and
`python -m trading_db check-traders --fill-in` does it for everyone (after users are added in SSMS, say).
Only blank rows are filled in: an order already given to someone keeps them.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from .connection import Connection
from .migrate import SCHEMA

TABLES = ("orders", "order_events")


class UnknownUser(NamedTuple):
    exchange_user: str  # the ElectronX login on the orders, "" if the exchange sent none
    orders: int  # orders from the screen recorded without a trader
    last_seen: datetime | None
    owner: str | None  # the user with this login now (the rows can be filled in), or None: nobody here


def fill_in_traders(conn: Connection, user_id: str | None = None) -> dict[str, int]:
    """Give screen orders recorded without a trader to the user with their ElectronX login (only `user_id`,
    if given). Returns the rows changed per table. The caller commits."""
    only = " AND u.user_id = %(id)s" if user_id else ""
    params = {"id": user_id} if user_id else None
    cur = conn.cursor()
    changed = {}
    for table in TABLES:
        cur.execute(
            f"UPDATE x SET trader = u.user_id FROM {SCHEMA}.{table} AS x"
            f" JOIN {SCHEMA}.users AS u ON u.user_id = LOWER(x.exchange_user)"
            f" WHERE x.source = 'electronx' AND x.trader = N''{only}",
            params,
        )
        changed[table] = cur.rowcount
    return changed


def unknown_users(conn: Connection) -> list[UnknownUser]:
    """ElectronX logins with screen orders recorded without a trader, most recent first."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT o.exchange_user, COUNT(*), MAX(COALESCE(o.updated_at, o.created_at)), MIN(u.user_id)"
        f" FROM {SCHEMA}.orders AS o LEFT JOIN {SCHEMA}.users AS u ON u.user_id = LOWER(o.exchange_user)"
        f" WHERE o.source = 'electronx' AND o.trader = N'' GROUP BY o.exchange_user ORDER BY 3 DESC"
    )
    return [UnknownUser(user or "", int(n), last, owner) for user, n, last, owner in cur.fetchall()]


def traders_without_account(conn: Connection) -> list[str]:
    """Active traders with no ElectronX account to trade on: orders from the app are refused for them."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT u.user_id FROM {SCHEMA}.users AS u JOIN {SCHEMA}.user_roles AS r ON r.user_id = u.user_id"
        f" WHERE u.active = 1 AND r.role = 'trader'"
        f" AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.account_access AS a WHERE a.user_id = u.user_id)"
        f" ORDER BY u.user_id"
    )
    return [row[0] for row in cur.fetchall()]
