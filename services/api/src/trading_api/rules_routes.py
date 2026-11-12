"""Trading rules in the app (see trading_db.changes):

* GET  /api/rules                              every rule (risk, management) or a trader's own
* GET  /api/rules/changes                      the audit trail, newest first (risk, management)
* POST /api/rules/changes                      propose a change (limits.edit)
* POST /api/rules/changes/{id}/approve         apply someone else's change (limits.approve)
* POST /api/rules/changes/{id}/reject          turn someone else's change down (limits.approve)
* POST /api/rules/changes/{id}/withdraw        take back your own change (limits.edit)

No `from __future__ import annotations`: FastAPI reads the Depends(...) annotations at runtime.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import closing
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from trading_core.rules import ANY
from trading_db import Connection, DbError, DbSettings, connect
from trading_db.changes import (
    ChangeError,
    ProposedChange,
    approve,
    current_rules,
    list_changes,
    propose,
    reject,
    withdraw,
)

from .auth import User

log = logging.getLogger("api.rules")


class ChangeRequest(BaseModel):
    kind: Literal["user", "entitlement", "limit"]
    action: Literal["set", "delete"] = "set"
    trader_id: str
    market: str = ANY
    product: str = ANY
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class Decision(BaseModel):
    note: str = ""


def add_rule_routes(
    app: FastAPI,
    db: DbSettings | None,
    current_user: Callable[..., User],
    require: Callable[[str], Callable[..., User]],
) -> None:
    async def with_db(work: Callable[[Connection], Any]) -> Any:
        """Run `work` on its own connection in a worker thread (pymssql blocks)."""
        if db is None:
            raise HTTPException(status_code=503, detail="no database is configured (services/db.env)")

        def run() -> Any:
            with closing(connect(db, appname="api")) as conn:
                return work(conn)

        try:
            return await asyncio.to_thread(run)
        except ChangeError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except DbError as exc:
            log.error("database error: %s", exc)
            raise HTTPException(status_code=503, detail="the database is unavailable") from exc

    @app.get("/api/rules")
    async def rules(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        """Risk and management see every rule; a trader sees the rows that apply to them."""
        sees_all = user.can("risk.view.all")
        if not sees_all and not user.can("orders.trade"):
            raise HTTPException(status_code=403, detail="missing permission risk.view.all")
        found = await with_db(current_rules)
        if not sees_all:
            found["users"] = [u for u in found["users"] if u["user_id"] == user.id]
            for table in ("entitlements", "limits"):
                found[table] = [r for r in found[table] if r["trader_id"] in (user.id, ANY)]
        return found | {"can_edit": user.can("limits.edit"), "can_approve": user.can("limits.approve")}

    @app.get("/api/rules/changes")
    async def changes(user: Annotated[User, Depends(require("risk.view.all"))]) -> list[dict[str, Any]]:
        return await with_db(list_changes)

    @app.post("/api/rules/changes")
    async def propose_change(
        body: ChangeRequest, user: Annotated[User, Depends(require("limits.edit"))]
    ) -> dict[str, Any]:
        change = ProposedChange(
            body.kind, body.action, body.trader_id, body.market, body.product, body.values, body.reason
        )
        change_id = await with_db(lambda conn: propose(conn, user.id, change))
        log.warning(
            "rule change %d proposed by %s: %s %s %s", change_id, user.id, body.action, body.kind, body.trader_id
        )
        return {"change_id": change_id, "status": "pending"}

    @app.post("/api/rules/changes/{change_id}/approve")
    async def approve_change(
        change_id: int, user: Annotated[User, Depends(require("limits.approve"))]
    ) -> dict[str, Any]:
        await with_db(lambda conn: approve(conn, change_id, user.id))
        log.warning("rule change %d approved by %s", change_id, user.id)
        return {"change_id": change_id, "status": "approved"}

    @app.post("/api/rules/changes/{change_id}/reject")
    async def reject_change(
        change_id: int, body: Decision, user: Annotated[User, Depends(require("limits.approve"))]
    ) -> dict[str, Any]:
        await with_db(lambda conn: reject(conn, change_id, user.id, body.note))
        return {"change_id": change_id, "status": "rejected"}

    @app.post("/api/rules/changes/{change_id}/withdraw")
    async def withdraw_change(change_id: int, user: Annotated[User, Depends(require("limits.edit"))]) -> dict[str, Any]:
        await with_db(lambda conn: withdraw(conn, change_id, user.id))
        return {"change_id": change_id, "status": "withdrawn"}
