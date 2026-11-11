"""Changes to the trading rules, made from the app: proposed by one person, approved by another.

Nobody changes electronx.traders, entitlements or limits from the app directly. A change is proposed
(propose), recording the rule as it is now and as it would be. Only someone else may approve it
(approve), which applies it in the same transaction and marks it approved; it can also be rejected,
or withdrawn by whoever proposed it. Everything stays in electronx.rule_changes as the audit trail.

If the rule was changed after a proposal (by another approved change, or in SSMS), approving it would
silently undo that, so the proposal becomes 'stale' instead: propose it again from the current rule.

Each ElectronX trader id belongs to one trader at most. Approving one also gives that trader the
orders from the ElectronX screen already recorded under it without a trader (attribution.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from trading_core import Market
from trading_core.rules import ANY, LIMIT_NAMES, TRADER_WIDE

from .attribution import fill_in_traders
from .connection import Connection
from .migrate import SCHEMA

KINDS = ("trader", "entitlement", "limit")
MARKETS = {m.value for m in Market} | {ANY}
PRODUCT = re.compile(r"^(\*|(BFUT|BOPT)\d*)$")
WHOLE_LOTS = ("max_order_qty", "max_open_orders", "max_position")
TRADER_FIELDS = ("exchange_trader_id", "active", "note")


class ChangeError(Exception):
    """A change that cannot be made; status is the HTTP status the api answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ProposedChange:
    kind: str  # trader, entitlement or limit
    action: str  # set (add or change) or delete
    trader_id: str  # or "*" (every trader) for entitlements and limits
    market: str = ANY
    product: str = ANY
    values: dict[str, Any] = field(default_factory=dict)  # trader: TRADER_FIELDS; limit: the limits
    reason: str = ""


def validate(change: ProposedChange) -> ProposedChange:
    """The change, tidied, or ChangeError(400) saying what is wrong with it."""
    kind, action = change.kind, change.action
    if kind not in KINDS:
        raise ChangeError(400, f"kind must be one of {', '.join(KINDS)}")
    if action not in ("set", "delete"):
        raise ChangeError(400, "action must be set or delete")
    trader = change.trader_id.strip()
    if not trader or len(trader) > 128:
        raise ChangeError(400, "trader_id is required (at most 128 characters)")
    reason = change.reason.strip()
    if not reason:
        raise ChangeError(400, "give a reason: it is kept with the change")
    market, product = change.market.strip().upper(), change.product.strip().upper()
    values = dict(change.values)
    if kind == "trader":
        if trader == ANY:
            raise ChangeError(400, "a trader needs a real user id, not *")
        market = product = ANY
        unknown = set(values) - set(TRADER_FIELDS)
        if unknown:
            raise ChangeError(400, f"unknown fields for a trader: {', '.join(sorted(unknown))}")
        if "active" in values:
            values["active"] = bool(values["active"])
        if values.get("exchange_trader_id") is not None:
            values["exchange_trader_id"] = str(values["exchange_trader_id"]).strip() or None
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
    if kind == "trader":
        cur.execute(
            f"SELECT exchange_trader_id, active, note FROM {SCHEMA}.traders{hint} WHERE trader_id = %(trader_id)s",
            key,
        )
        row = cur.fetchone()
        return None if row is None else {"exchange_trader_id": row[0], "active": bool(row[1]), "note": row[2]}
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
    if change.kind == "trader":
        base = before or {"exchange_trader_id": None, "active": True, "note": ""}
        return base | change.values
    if change.kind == "entitlement":
        return {}
    return dict(change.values)  # a limit row becomes exactly the values given; blank ones are not set


def _check_id_free(conn: Connection, trader_id: str, after: dict[str, Any] | None) -> None:
    """ChangeError(409) if another trader already has the ElectronX id this change gives `trader_id`."""
    exchange_id = (after or {}).get("exchange_trader_id")
    if not exchange_id:
        return
    cur = conn.cursor()
    cur.execute(
        f"SELECT trader_id, exchange_trader_id FROM {SCHEMA}.traders"
        " WHERE exchange_trader_id = %(id)s AND trader_id <> %(trader)s",  # in any case, as SQL Server compares
        {"id": exchange_id, "trader": trader_id},
    )
    row = cur.fetchone()
    if row is not None:
        raise ChangeError(
            409, f"{row[0]} already has the ElectronX id {row[1]}: each ElectronX id belongs to one trader"
        )


def current_rules(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    """Every trader, entitlement and limit row, for showing in the app."""
    cur = conn.cursor(as_dict=True)
    cur.execute(f"SELECT trader_id, exchange_trader_id, active, note FROM {SCHEMA}.traders ORDER BY trader_id")
    traders = [dict(r, active=bool(r["active"])) for r in cur.fetchall()]
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
    return {"traders": traders, "entitlements": entitlements, "limits": limits}


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
    if change.kind == "trader":
        _check_id_free(conn, change.trader_id, after)
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
        key = _key(row)
        before = None if row["before_json"] is None else json.loads(row["before_json"])
        if _current(conn, row["kind"], key, lock=True) != before:
            _decide(conn, change_id, "stale", user, "the rule changed after this was proposed; propose it again")
            conn.commit()
            raise ChangeError(409, "the rule changed after this was proposed, so it was not applied; propose it again")
        after = None if row["after_json"] is None else json.loads(row["after_json"])
        if row["kind"] == "trader":
            _check_id_free(conn, key["trader_id"], after)  # someone may have been given it since
        _apply(conn, row["kind"], key, before, after)
        if row["kind"] == "trader" and after and after.get("exchange_trader_id"):
            fill_in_traders(conn, after["exchange_trader_id"])  # their screen orders recorded before now
        _decide(conn, change_id, "approved", user)
        conn.commit()
    except Exception:
        conn.rollback()  # after a stale commit this undoes nothing; otherwise nothing half-applied stays
        raise


def _apply(conn: Connection, kind: str, key: dict[str, str], before: dict | None, after: dict | None) -> None:
    cur = conn.cursor()
    table = {"trader": "traders", "entitlement": "entitlements", "limit": "limits"}[kind]
    where = (
        "trader_id = %(trader_id)s"
        if kind == "trader"
        else ("trader_id = %(trader_id)s AND market = %(market)s AND product = %(product)s")
    )
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
        names = (["trader_id"] if kind == "trader" else ["trader_id", "market", "product"]) + columns
        cur.execute(
            f"INSERT {SCHEMA}.{table} ({', '.join(names)}) VALUES ({', '.join(f'%({n})s' for n in names)})", params
        )
    else:
        sets = ", ".join(f"{c} = %({c})s" for c in columns)
        cur.execute(f"UPDATE {SCHEMA}.{table} SET {sets}, updated_at = sysutcdatetime() WHERE {where}", params)


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
