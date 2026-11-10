"""Internal HTTP endpoint the api calls to place, cancel and list orders.

It listens on localhost only and every request must carry the shared X-Internal-Token. The api has
already checked who the user is and what they may do; this endpoint trusts the `trader` it is given.

No `from __future__ import annotations` here: FastAPI reads the annotations at runtime.
"""

import hmac
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .app import OrderEntryApp, OrderError


class NewOrder(BaseModel):
    trader: str
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int = Field(gt=0)
    price: float
    tif: Literal["DAY", "GTC", "IOC", "FOK"] = "DAY"


class CancelRequest(BaseModel):
    trader: str


def create_control_app(
    oe: OrderEntryApp, token: str, load_instrument: Callable[[str], Awaitable[None]] | None = None
) -> FastAPI:
    """`load_instrument` brings a contract's tick size, limits and state up to date before an order for it
    is checked."""

    def check_token(x_internal_token: Annotated[str, Header()] = "") -> None:
        if not hmac.compare_digest(x_internal_token, token):
            raise HTTPException(status_code=401, detail="bad internal token")

    app = FastAPI(title="orderentry (internal)", dependencies=[Depends(check_token)])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"fix": "logged_on" if oe.session is not None and oe.session.logged_on else "down"}

    @app.post("/orders")
    async def new_order(body: NewOrder) -> dict[str, Any]:
        if load_instrument is not None:
            await load_instrument(body.symbol)
        return oe.submit(body.trader, body.symbol, body.side, body.qty, body.price, body.tif).to_dict()

    @app.post("/orders/{cl_ord_id}/cancel")
    def cancel(cl_ord_id: str, body: CancelRequest) -> dict[str, Any]:
        try:
            return oe.cancel(body.trader, cl_ord_id).to_dict()
        except OrderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/orders")
    def list_orders(trader: str | None = None) -> list[dict[str, Any]]:
        return [o.to_dict() for o in oe.orders_for(trader)]

    @app.get("/positions")
    def positions() -> dict[str, dict[str, int]]:
        """Net lots per trader and symbol, from orderentry's own fills."""
        return oe.positions_by_trader()

    return app
