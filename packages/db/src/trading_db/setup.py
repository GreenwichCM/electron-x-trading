"""Setting up a new database (production's): its first users, before anyone can approve a change.

Every change to users goes through the Trading rules page, proposed by one person and approved by
another with the risk role. A new database has nobody to do that yet, so `add_user` adds people directly,
and only until two active users have the risk role; after that it refuses and the page takes over.
Each one is still recorded in electronx.rule_changes, as approved by "setup".
"""

from __future__ import annotations

import json

from .changes import ChangeError, ProposedChange, _after, _apply_user, _current, _key, validate
from .connection import Connection
from .migrate import SCHEMA

SETUP = "setup"  # requested_by and decided_by of the changes recorded here
RISK_USERS_NEEDED = 2  # enough to approve each other's changes on the Trading rules page


def risk_users(conn: Connection) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        f"SELECT u.user_id FROM {SCHEMA}.users u WITH (UPDLOCK, HOLDLOCK)"
        f" JOIN {SCHEMA}.user_roles r ON r.user_id = u.user_id"
        " WHERE r.role = 'risk' AND u.active = 1 ORDER BY u.user_id"
    )
    return [row[0] for row in cur.fetchall()]


def add_user(conn: Connection, user_id: str, roles: list[str], accounts: list[str], name: str = "") -> list[str]:
    """Add one user with their roles and ElectronX accounts; accounts not in electronx.accounts yet are
    added too (capacity 2). Returns the accounts added. ChangeError if setup is over or they exist."""
    try:
        if len(risk_users(conn)) >= RISK_USERS_NEEDED:
            raise ChangeError(
                409, "this database has its risk users already: add people on the Trading rules page instead"
            )
        values = {"roles": roles, "accounts": accounts} | ({"name": name} if name else {})
        change = validate(ProposedChange("user", "set", user_id, values=values, reason="setting up a new database"))
        if _current(conn, "user", _key(change), lock=True) is not None:
            raise ChangeError(409, f"{change.trader_id} is a user already: change them on the Trading rules page")
        after = _after(change, None)
        assert after is not None
        cur = conn.cursor()
        cur.execute(f"SELECT account_id FROM {SCHEMA}.accounts WITH (UPDLOCK, HOLDLOCK)")
        added = sorted(set(after["accounts"]) - {row[0] for row in cur.fetchall()})
        for account in added:
            cur.execute(f"INSERT {SCHEMA}.accounts (account_id) VALUES (%s)", (account,))
        _apply_user(conn, change.trader_id, None, after)
        cur.execute(
            f"INSERT {SCHEMA}.rule_changes (kind, action, trader_id, market, product, before_json, after_json,"
            " reason, status, requested_by, decided_by, decided_at)"
            " VALUES ('user', 'set', %(id)s, %(market)s, %(product)s, NULL, %(after)s, %(reason)s, 'approved',"
            " %(setup)s, %(setup)s, sysutcdatetime())",
            {
                "id": change.trader_id,
                "market": change.market,
                "product": change.product,
                "after": json.dumps(after, sort_keys=True),
                "reason": "added with `python -m trading_db add-user` while setting up the database",
                "setup": SETUP,
            },
        )
        conn.commit()
        return added
    except Exception:
        conn.rollback()
        raise


# Every table with data, children first; the schema and electronx.schema_migrations stay.
DATA_TABLES = (
    "order_events",
    "fills",
    "exchange_reports",
    "orders",
    "alerts",
    "stream_offsets",
    "rule_changes",
    "entitlements",
    "limits",
    "account_access",
    "user_roles",
    "users",
    "accounts",
    "environment",
)


def row_counts(conn: Connection) -> dict[str, int]:
    cur = conn.cursor()
    counts = {}
    for table in DATA_TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}")
        counts[table] = int(cur.fetchone()[0])
    conn.commit()
    return counts


def marked_as(conn: Connection) -> str | None:
    cur = conn.cursor()
    cur.execute(f"SELECT name FROM {SCHEMA}.environment")
    row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def clear_data(conn: Connection) -> dict[str, int]:
    """Delete every row of the electronx tables (test data from development, say), including the
    environment mark, so production can then start on it. Refuses a database marked as production's.
    Returns the rows deleted per table. Open `conn` with connect(..., check_environment=False)."""
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT name FROM {SCHEMA}.environment WITH (UPDLOCK, HOLDLOCK)")
        row = cur.fetchone()
        if row and row[0] == "production":
            raise ChangeError(409, "this database is production's: its orders and fills are not deleted")
        deleted = {}
        for table in DATA_TABLES:
            cur.execute(f"DELETE FROM {SCHEMA}.{table}")
            deleted[table] = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
