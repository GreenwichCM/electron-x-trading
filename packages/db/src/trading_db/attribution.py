"""Whose orders from the ElectronX screen are whose.

An order from the screen carries only the trader's ElectronX id (TargetSubID 57, `exchange_user`). The
recorder fills in `trader` from electronx.traders.exchange_trader_id as it records each update, so rows
recorded before anybody had that id are left blank. fill_in_traders gives those rows to whoever has the
id now: approving an ElectronX id on the Trading rules page does it for that id, and
`python -m trading_db check-traders --fill-in` does it for every id (after a change made in SSMS, say).
Only blank rows are filled in: an order already given to a trader keeps them.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from .connection import Connection
from .migrate import SCHEMA

TABLES = ("orders", "order_events")


class UnknownUser(NamedTuple):
    exchange_user: str  # the ElectronX id on the orders, "" if the exchange sent none
    orders: int  # orders from the screen recorded without a trader
    last_seen: datetime | None
    owner: str | None  # the trader who has this id now (the rows can be filled in), or None: nobody does


def fill_in_traders(conn: Connection, exchange_trader_id: str | None = None) -> dict[str, int]:
    """Give screen orders recorded without a trader to whoever now has their ElectronX id (only this id,
    if given). Returns the rows changed per table. The caller commits."""
    only = " AND t.exchange_trader_id = %(id)s" if exchange_trader_id else ""
    params = {"id": exchange_trader_id} if exchange_trader_id else None
    cur = conn.cursor()
    changed = {}
    for table in TABLES:
        cur.execute(
            f"UPDATE x SET trader = t.trader_id FROM {SCHEMA}.{table} AS x"
            f" JOIN {SCHEMA}.traders AS t ON t.exchange_trader_id = x.exchange_user"
            f" WHERE x.source = 'electronx' AND x.trader = N''{only}",
            params,
        )
        changed[table] = cur.rowcount
    return changed


def unknown_users(conn: Connection) -> list[UnknownUser]:
    """ElectronX users with screen orders recorded without a trader, most recent first."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT o.exchange_user, COUNT(*), MAX(COALESCE(o.updated_at, o.created_at)), MIN(t.trader_id)"
        f" FROM {SCHEMA}.orders AS o LEFT JOIN {SCHEMA}.traders AS t ON t.exchange_trader_id = o.exchange_user"
        f" WHERE o.source = 'electronx' AND o.trader = N'' GROUP BY o.exchange_user ORDER BY 3 DESC"
    )
    return [UnknownUser(user or "", int(n), last, owner) for user, n, last, owner in cur.fetchall()]


def traders_without_id(conn: Connection) -> list[str]:
    """Active traders with no ElectronX id: their screen orders cannot be told apart from anyone's."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT trader_id FROM {SCHEMA}.traders WHERE active = 1 AND exchange_trader_id IS NULL ORDER BY trader_id"
    )
    return [row[0] for row in cur.fetchall()]
