"""Changes to the trading rules, made from the app: proposed by one person, approved by another.

Nobody changes electronx.users, entitlements or limits from the app directly. A change is proposed
(propose), recording the rule as it is now and as it would be. Only someone else may approve it
(approve), which applies it in the same transaction and marks it approved; it can also be rejected,
or withdrawn by whoever proposed it. Everything stays in electronx.rule_changes as the audit trail.

If the rule was changed after a proposal (by another approved change, or in SSMS), approving it would
silently undo that, so the proposal becomes 'stale' instead: propose it again from the current rule.

A user is a person: their id is their ElectronX login (company email, lower case), and the change says
their name, roles, the ElectronX accounts they may trade on, and whether they are active. Approving a
user also gives them the orders from the ElectronX screen already recorded under their login without
a trader (attribution.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from trading_core import Market
from trading_core.rules import ANY, LIMIT_NAMES, ROLES, TRADER_WIDE

from .attribution import fill_in_traders
from .connection import Connection
from .migrate import SCHEMA

KINDS = ("user", "entitlement", "limit")
MARKETS = {m.value for m in Market} | {ANY}
PRODUCT = re.compile(r"^(\*|(BFUT|BOPT)\d*)$")
WHOLE_LOTS = ("max_order_qty", "max_open_orders", "max_position")
USER_FIELDS = ("name", "active", "roles", "accounts", "note")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChangeError(Exception):
    """A change that cannot be made; status is the HTTP status the api answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ProposedChange:
    kind: str  # user, entitlement or limit
    action: str  # set (add or change) or delete
    trader_id: str  # the user's id (their ElectronX login), or "*" (everyone) for entitlements and limits
    market: str = ANY
    product: str = ANY
    values: dict[str, Any] = field(default_factory=dict)  # user: USER_FIELDS; limit: the limits
    reason: str = ""


def validate(change: ProposedChange) -> ProposedChange:
    """The change, tidied, or ChangeError(400) saying what is wrong with it."""
    kind, action = change.kind, change.action
    if kind not in KINDS:
        raise ChangeError(400, f"kind must be one of {', '.join(KINDS)}")
    if action not in ("set", "delete"):
        raise ChangeError(400, "action must be set or delete")
    trader = change.trader_id.strip().lower()  # ids are ElectronX logins, kept in lower case
    if not trader or len(trader) > 128:
        raise ChangeError(400, "trader_id is required (at most 128 characters)")
    if trader != ANY and not EMAIL.match(trader):
        raise ChangeError(400, "a user's id is their company email, which is also their ElectronX login")
    reason = change.reason.strip()
    if not reason:
        raise ChangeError(400, "give a reason: it is kept with the change")
    market, product = change.market.strip().upper(), change.product.strip().upper()
    values = dict(change.values)
    if kind == "user":
        if trader == ANY:
            raise ChangeError(400, "a user needs their own id, not *")
        market = product = ANY
        values = _user_values(values)
    else:
        if market not in MARKETS:
            raise ChangeError(400, f"market must be one of {', '.join(sorted(MARKETS))}")
        if not PRODUCT.match(product):
            raise ChangeError(400, "product must be * or a product code such as BFUT500 or BFUT100")
    if kind == "entitlement" and values:
        raise ChangeError(400, "an entitlement has no values: it either exists or not")
    if kind == "limit":
        values = _limit_values(values, market, product, action)
    return ProposedChange(kind, action, trader, market, product, values if action == "set" else {}, reason[:400])


def _names(value: Any) -> list[str]:
    """A list of names from a list or a comma-separated string, tidied and without repeats."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    return sorted({str(v).strip() for v in items if str(v).strip()})


def _user_values(values: dict[str, Any]) -> dict[str, Any]:
    unknown = set(values) - set(USER_FIELDS)
    if unknown:
        raise ChangeError(400, f"unknown fields for a user: {', '.join(sorted(unknown))}")
    out = dict(values)
    if "active" in out:
        out["active"] = bool(out["active"])
    if "name" in out:
        out["name"] = str(out["name"] or "").strip()[:128]
    if "note" in out:
        out["note"] = str(out["note"] or "").strip()[:400]
    if "roles" in out:
        out["roles"] = [r.lower() for r in _names(out["roles"])]
        bad = set(out["roles"]) - set(ROLES)
        if bad:
            raise ChangeError(400, f"roles are {', '.join(ROLES)}, not {', '.join(sorted(bad))}")
    if "accounts" in out:
        out["accounts"] = [a.upper() for a in _names(out["accounts"])]
    return out


def _limit_values(values: dict[str, Any], market: str, product: str, action: str) -> dict[str, Any]:
    unknown = set(values) - set(LIMIT_NAMES)
    if unknown:
        raise ChangeError(400, f"unknown limits: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for name in LIMIT_NAMES:
        value = values.get(name)
        if value is None or value == "":
            out[name] = None
            continue
        try:
            number = float(value)
        except TypeError, ValueError:
            raise ChangeError(400, f"{name} must be a number") from None
        if number < 0:
            raise ChangeError(400, f"{name} cannot be negative")
        if name in WHOLE_LOTS:
            if number != int(number):
                raise ChangeError(400, f"{name} must be a whole number")
            number = int(number)
        if name in TRADER_WIDE and (market, product) != (ANY, ANY):
            raise ChangeError(400, f"{name} is per trader: set it on a row for every market and product (*, *)")
        out[name] = number
    if action == "set" and all(v is None for v in out.values()):
        raise ChangeError(400, "set at least one limit (or remove the row instead)")
    return out


# ---- reading the rules as they are ---------------------------------------------------------------


def _key(change: ProposedChange | dict) -> dict[str, str]:
    get = change.get if isinstance(change, dict) else lambda k: getattr(change, k)
    return {"trader_id": get("trader_id"), "market": get("market"), "product": get("product")}


def _current(conn: Connection, kind: str, key: dict[str, str], lock: bool = False) -> dict[str, Any] | None:
    """The rule as it is now, as a plain dict, or None if there is none."""
    hint = " WITH (UPDLOCK, HOLDLOCK)" if lock else ""  # held until commit, so nobody changes it meanwhile
    cur = conn.cursor()
    if kind == "user":
        cur.execute(f"SELECT display_name, active, note FROM {SCHEMA}.users{hint} WHERE user_id = %(trader_id)s", key)
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(f"SELECT role FROM {SCHEMA}.user_roles{hint} WHERE user_id = %(trader_id)s ORDER BY role", key)
        roles = [r[0] for r in cur.fetchall()]
        cur.execute(
            f"SELECT account_id FROM {SCHEMA}.account_access{hint} WHERE user_id = %(trader_id)s ORDER BY account_id",
            key,
        )
        accounts = [r[0] for r in cur.fetchall()]
        return {"name": row[0], "active": bool(row[1]), "roles": roles, "accounts": accounts, "note": row[2]}
    where = "trader_id = %(trader_id)s AND market = %(market)s AND product = %(product)s"
    if kind == "entitlement":
        cur.execute(f"SELECT 1 FROM {SCHEMA}.entitlements{hint} WHERE {where}", key)
        return {} if cur.fetchone() else None
    cur.execute(f"SELECT {', '.join(LIMIT_NAMES)} FROM {SCHEMA}.limits{hint} WHERE {where}", key)
    row = cur.fetchone()
    if row is None:
        return None
    return {
        n: (None if v is None else int(v) if n in WHOLE_LOTS else float(v))
        for n, v in zip(LIMIT_NAMES, row, strict=True)
    }


def _after(change: ProposedChange, before: dict[str, Any] | None) -> dict[str, Any] | None:
    if change.action == "delete":
        return None
    if change.kind == "user":
        name = change.trader_id.split("@")[0]
        base = before or {"name": name, "active": True, "roles": [], "accounts": [], "note": ""}
        after = base | change.values
        after["name"] = after["name"] or name
        return after
    if change.kind == "entitlement":
        return {}
    return dict(change.values)  # a limit row becomes exactly the values given; blank ones are not set


def _check_accounts(conn: Connection, after: dict[str, Any] | None) -> None:
    """ChangeError(409) if the change lets a user trade on an account electronx.accounts does not have."""
    wanted = (after or {}).get("accounts") or []
    if not wanted:
        return
    cur = conn.cursor()
    cur.execute(f"SELECT account_id FROM {SCHEMA}.accounts")
    missing = sorted(set(wanted) - {row[0] for row in cur.fetchall()})
    if missing:
        raise ChangeError(409, f"no ElectronX account {', '.join(missing)} in electronx.accounts")


def current_rules(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    """Every trader, entitlement and limit row, for showing in the app."""
    cur = conn.cursor(as_dict=True)
    cur.execute(f"SELECT user_id, display_name AS name, active, note FROM {SCHEMA}.users ORDER BY user_id")
    users = [dict(r, active=bool(r["active"]), roles=[], accounts=[]) for r in cur.fetchall()]
    by_id = {u["user_id"]: u for u in users}
    cur.execute(f"SELECT user_id, role FROM {SCHEMA}.user_roles ORDER BY role")
    for r in cur.fetchall():
        by_id[r["user_id"]]["roles"].append(r["role"])
    cur.execute(f"SELECT user_id, account_id FROM {SCHEMA}.account_access ORDER BY account_id")
    for r in cur.fetchall():
        by_id[r["user_id"]]["accounts"].append(r["account_id"])
    cur.execute(f"SELECT account_id, name, capacity, active FROM {SCHEMA}.accounts ORDER BY account_id")
    accounts = [dict(r, active=bool(r["active"])) for r in cur.fetchall()]
    cur.execute(f"SELECT trader_id, market, product FROM {SCHEMA}.entitlements ORDER BY trader_id, market, product")
    entitlements = list(cur.fetchall())
    cur.execute(
        f"SELECT trader_id, market, product, {', '.join(LIMIT_NAMES)} FROM {SCHEMA}.limits"
        " ORDER BY trader_id, market, product"
    )
    limits = []
    for r in cur.fetchall():
        for n in LIMIT_NAMES:
            if r[n] is not None:
                r[n] = int(r[n]) if n in WHOLE_LOTS else float(r[n])
        limits.append(r)
    return {"users": users, "accounts": accounts, "entitlements": entitlements, "limits": limits}


# ---- proposing and deciding ------------------------------------------------------------------------


def _dump(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def propose(conn: Connection, user: str, change: ProposedChange) -> int:
    """Record a proposed change; returns its id. Nothing about the rules changes yet."""
    change = validate(change)
    before = _current(conn, change.kind, _key(change))
    after = _after(change, before)
    if change.action == "delete" and before is None:
        raise ChangeError(409, "there is no such rule to remove")
    if after == before:
        raise ChangeError(409, "that is already the rule")
    if change.kind == "user":
        _check_accounts(conn, after)
    cur = conn.cursor()
    cur.execute(
        f"INSERT {SCHEMA}.rule_changes (kind, action, trader_id, market, product, before_json, after_json, reason,"
        " requested_by) OUTPUT INSERTED.change_id"
        " VALUES (%(kind)s, %(action)s, %(trader_id)s, %(market)s, %(product)s, %(before)s, %(after)s, %(reason)s,"
        " %(user)s)",
        {
            "kind": change.kind,
            "action": change.action,
            **_key(change),
            "before": _dump(before),
            "after": _dump(after),
            "reason": change.reason,
            "user": user,
        },
    )
    change_id = cur.fetchone()[0]
    conn.commit()
    return int(change_id)


def _pending(conn: Connection, change_id: int) -> dict[str, Any]:
    cur = conn.cursor(as_dict=True)
    cur.execute(f"SELECT * FROM {SCHEMA}.rule_changes WITH (UPDLOCK, HOLDLOCK) WHERE change_id = %s", (change_id,))
    row = cur.fetchone()
    if row is None:
        raise ChangeError(404, f"no change {change_id}")
    if row["status"] != "pending":
        raise ChangeError(409, f"change {change_id} is already {row['status']}")
    return row


def _decide(conn: Connection, change_id: int, status: str, user: str, note: str = "") -> None:
    conn.cursor().execute(
        f"UPDATE {SCHEMA}.rule_changes SET status = %s, decided_by = %s, decided_at = sysutcdatetime(),"
        " decision_note = %s WHERE change_id = %s",
        (status, user, note[:400], change_id),
    )


def approve(conn: Connection, change_id: int, user: str) -> None:
    """Apply a proposed change, if `user` is not who proposed it and the rule has not changed since."""
    try:
        row = _pending(conn, change_id)
        if row["requested_by"] == user:
            raise ChangeError(403, "a change needs a second person: someone else has to approve it")
        if row["kind"] not in KINDS:
            raise ChangeError(409, f"a {row['kind']} change no longer fits the rules: propose it again")
        key = _key(row)
        before = None if row["before_json"] is None else json.loads(row["before_json"])
        if _current(conn, row["kind"], key, lock=True) != before:
            _decide(conn, change_id, "stale", user, "the rule changed after this was proposed; propose it again")
            conn.commit()
            raise ChangeError(409, "the rule changed after this was proposed, so it was not applied; propose it again")
        after = None if row["after_json"] is None else json.loads(row["after_json"])
        if row["kind"] == "user":
            _check_accounts(conn, after)  # an account may have gone since
        _apply(conn, row["kind"], key, before, after)
        if row["kind"] == "user" and after:
            fill_in_traders(conn, key["trader_id"])  # their screen orders recorded before they were added
        _decide(conn, change_id, "approved", user)
        conn.commit()
    except Exception:
        conn.rollback()  # after a stale commit this undoes nothing; otherwise nothing half-applied stays
        raise


def _apply(conn: Connection, kind: str, key: dict[str, str], before: dict | None, after: dict | None) -> None:
    if kind == "user":
        _apply_user(conn, key["trader_id"], before, after)
        return
    cur = conn.cursor()
    table = {"entitlement": "entitlements", "limit": "limits"}[kind]
    where = "trader_id = %(trader_id)s AND market = %(market)s AND product = %(product)s"
    if after is None:
        cur.execute(f"DELETE FROM {SCHEMA}.{table} WHERE {where}", key)
        return
    if kind == "entitlement":
        cur.execute(
            f"INSERT {SCHEMA}.entitlements (trader_id, market, product)"
            " VALUES (%(trader_id)s, %(market)s, %(product)s)",
            key,
        )
        return
    params = key | after
    columns = list(after)
    if before is None:
        names = ["trader_id", "market", "product"] + columns
        cur.execute(
            f"INSERT {SCHEMA}.{table} ({', '.join(names)}) VALUES ({', '.join(f'%({n})s' for n in names)})", params
        )
    else:
        sets = ", ".join(f"{c} = %({c})s" for c in columns)
        cur.execute(f"UPDATE {SCHEMA}.{table} SET {sets}, updated_at = sysutcdatetime() WHERE {where}", params)


def _apply_user(conn: Connection, user_id: str, before: dict | None, after: dict | None) -> None:
    """The user row, then their roles and accounts exactly as `after` lists them."""
    cur = conn.cursor()
    who = {"id": user_id}
    if after is None:
        cur.execute(f"DELETE FROM {SCHEMA}.users WHERE user_id = %(id)s", who)  # roles and access go too
        return
    row = who | {"name": after["name"], "active": after["active"], "note": after["note"]}
    if before is None:
        cur.execute(
            f"INSERT {SCHEMA}.users (user_id, display_name, active, note)"
            " VALUES (%(id)s, %(name)s, %(active)s, %(note)s)",
            row,
        )
    else:
        cur.execute(
            f"UPDATE {SCHEMA}.users SET display_name = %(name)s, active = %(active)s, note = %(note)s,"
            " updated_at = sysutcdatetime() WHERE user_id = %(id)s",
            row,
        )
    cur.execute(f"DELETE FROM {SCHEMA}.user_roles WHERE user_id = %(id)s", who)
    for role in after["roles"]:
        cur.execute(f"INSERT {SCHEMA}.user_roles (user_id, role) VALUES (%(id)s, %(role)s)", who | {"role": role})
    cur.execute(f"DELETE FROM {SCHEMA}.account_access WHERE user_id = %(id)s", who)
    for account in after["accounts"]:
        cur.execute(
            f"INSERT {SCHEMA}.account_access (user_id, account_id) VALUES (%(id)s, %(account)s)",
            who | {"account": account},
        )


def reject(conn: Connection, change_id: int, user: str, note: str = "") -> None:
    try:
        row = _pending(conn, change_id)
        if row["requested_by"] == user:
            raise ChangeError(403, "withdraw your own change instead of rejecting it")
        _decide(conn, change_id, "rejected", user, note)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def withdraw(conn: Connection, change_id: int, user: str) -> None:
    try:
        row = _pending(conn, change_id)
        if row["requested_by"] != user:
            raise ChangeError(403, "only whoever proposed a change can withdraw it")
        _decide(conn, change_id, "withdrawn", user)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def list_changes(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    """The latest changes, newest first, with before and after as dicts."""
    cur = conn.cursor(as_dict=True)
    cur.execute(f"SELECT TOP (%s) * FROM {SCHEMA}.rule_changes ORDER BY change_id DESC", (limit,))
    out = []
    for r in cur.fetchall():
        before, after = r.pop("before_json"), r.pop("after_json")
        r["before"] = None if before is None else json.loads(before)
        r["after"] = None if after is None else json.loads(after)
        out.append(r)
    return out
