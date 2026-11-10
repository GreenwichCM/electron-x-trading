"""The FastAPI application: REST under /api, WebSockets under /ws.

No `from __future__ import annotations` here: FastAPI reads the Depends(...) annotations at runtime,
and string annotations could not see the dependency functions defined inside create_app.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from trading_core import ALERTS_STREAM, BLOCKS_KEY, KILLSWITCH_KEY, KillSwitchState, TraderBlock, decode_blocks

from .auth import Authenticator, DevAuthenticator, User
from .hub import ClientFeed, MarketDataHub
from .order_hub import OrderHub
from .orderentry_client import OrderEntryClient, OrderEntryError, OrderEntryUnavailable
from .rules_routes import add_rule_routes
from .settings import ConfigError, Settings

log = logging.getLogger("api")

WS_AUTH_TIMEOUT_S = 5.0
WS_UNAUTHORIZED = 4401  # application close codes live in 4000-4999
WS_FORBIDDEN = 4403


class DevLoginRequest(BaseModel):
    user_id: str


class NewOrderRequest(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int = Field(gt=0)
    price: float
    tif: Literal["DAY", "GTC", "IOC", "FOK"] = "DAY"


class KillSwitchRequest(BaseModel):
    active: bool
    reason: str = ""


class BlockRequest(BaseModel):
    blocked: bool
    reason: str = ""


DC_POSITIONS_KEY = "dc:positions"
RISK_SNAPSHOT_KEY = "risk:snapshot"


def create_app(
    redis: Redis | None = None,
    settings: Settings | None = None,
    authenticator: Authenticator | None = None,
    orderentry: OrderEntryClient | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own `redis` (fakeredis), `authenticator` and `orderentry`."""
    settings = settings or Settings.from_env()
    dev = DevAuthenticator(settings.dev_secret) if settings.dev_login else None
    auth = authenticator or dev
    if auth is None:
        raise ConfigError("no login configured: set API_DEV_LOGIN=Y for development")
    oe = orderentry or OrderEntryClient(settings.oe_url, settings.oe_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis if redis is not None else Redis.from_url(settings.redis_url)
        app.state.redis = client
        app.state.hub = MarketDataHub(client)
        app.state.orders = OrderHub(client)
        tasks = [
            asyncio.create_task(app.state.hub.run(), name="md-hub"),
            asyncio.create_task(app.state.orders.run(), name="order-hub"),
        ]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if orderentry is None:
            await oe.aclose()
        if redis is None:
            await client.aclose()

    app = FastAPI(title="ElectronX trading api", lifespan=lifespan)

    # ---- authentication and permissions ----------------------------------------------------

    def current_user(request: Request) -> User:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        user = auth.authenticate(token) if scheme.lower() == "bearer" and token else None
        if user is None:
            raise HTTPException(status_code=401, detail="not signed in", headers={"WWW-Authenticate": "Bearer"})
        return user

    def require(permission: str) -> Callable[[User], User]:
        def check(user: Annotated[User, Depends(current_user)]) -> User:
            if not user.can(permission):
                raise HTTPException(status_code=403, detail=f"missing permission {permission}")
            return user

        return check

    def order_scope(user: User) -> str | None:
        """Whose orders `user` may see: None = everyone's, else their own id. Raises 403 if neither."""
        if user.can("orders.view.all"):
            return None
        if user.can("orders.view.own"):
            return user.id
        raise HTTPException(status_code=403, detail="missing permission orders.view.own")

    async def call_orderentry(coro: Any) -> Any:
        try:
            return await coro
        except OrderEntryUnavailable as exc:
            log.error("orderentry unavailable: %s", exc)
            raise HTTPException(status_code=503, detail="order entry is unavailable") from exc
        except OrderEntryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # ---- REST ------------------------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if dev is not None:

        @app.get("/api/dev/users")
        async def dev_users() -> list[dict[str, Any]]:
            return [u.to_dict() for u in dev.users.values()]

        @app.post("/api/dev/login")
        async def dev_login(body: DevLoginRequest) -> dict[str, Any]:
            user = dev.users.get(body.user_id)
            if user is None:
                raise HTTPException(status_code=404, detail=f"no test user {body.user_id}")
            return {"token": dev.issue(user.id), "user": user.to_dict()}

    @app.get("/api/me")
    async def me(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        return user.to_dict()

    @app.get("/api/instruments")
    async def instruments(user: Annotated[User, Depends(require("md.view"))]) -> list[dict[str, Any]]:
        found = await app.state.hub.instruments()
        return [found[s] for s in sorted(found)]

    @app.get("/api/md/{symbol}")
    async def latest_book(symbol: str, user: Annotated[User, Depends(require("md.view"))]) -> dict[str, Any]:
        book = (await app.state.hub.latest()).get(symbol)
        if book is None:
            raise HTTPException(status_code=404, detail=f"no market data for {symbol}")
        return book

    @app.get("/api/orders")
    async def list_orders(user: Annotated[User, Depends(current_user)]) -> list[dict[str, Any]]:
        return await call_orderentry(oe.list(order_scope(user)))

    @app.post("/api/orders")
    async def new_order(
        body: NewOrderRequest, user: Annotated[User, Depends(require("orders.trade"))]
    ) -> dict[str, Any]:
        # The trader is always the signed-in user: nobody can place an order in someone else's name.
        return await call_orderentry(oe.submit(user.id, body.symbol, body.side, body.qty, body.price, body.tif))

    @app.post("/api/orders/{cl_ord_id}/cancel")
    async def cancel_order(cl_ord_id: str, user: Annotated[User, Depends(require("orders.trade"))]) -> dict[str, Any]:
        return await call_orderentry(oe.cancel(user.id, cl_ord_id))

    @app.get("/api/killswitch")
    async def kill_switch(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        """Everyone signed in may see whether trading is halted, and why."""
        return KillSwitchState.from_json(await app.state.redis.get(KILLSWITCH_KEY)).__dict__

    @app.post("/api/killswitch")
    async def set_kill_switch(
        body: KillSwitchRequest, user: Annotated[User, Depends(require("killswitch.trip"))]
    ) -> dict[str, Any]:
        if body.active and not body.reason.strip():
            raise HTTPException(status_code=400, detail="give a reason for halting trading")
        state = KillSwitchState(
            active=body.active,
            reason=body.reason.strip(),
            by=user.id,
            time=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        await app.state.redis.set(KILLSWITCH_KEY, state.to_json())
        await app.state.redis.xadd(ALERTS_STREAM, {"data": state.to_json()}, maxlen=10_000)
        log.warning("kill switch %s by %s: %s", "ON" if state.active else "OFF", user.id, state.reason)
        return state.__dict__

    @app.get("/api/positions")
    async def positions(user: Annotated[User, Depends(require("risk.view.all"))]) -> dict[str, Any]:
        """The exchange's positions for the whole account (drop copy) next to orderentry's, per trader.

        A contract with a final settlement price is closed, so it is left out (listed under `settled`) as
        long as both sides agree on it; a break stays visible.
        """
        raw = await app.state.redis.get(DC_POSITIONS_KEY)
        dc = json.loads(raw) if raw else {"positions": {}, "connected": False, "time": ""}
        try:
            by_trader = await oe.positions()
        except OrderEntryUnavailable, OrderEntryError:
            by_trader = None
        exchange = dict(dc["positions"])
        ours: dict[str, int] = {}
        for held in (by_trader or {}).values():
            for symbol, qty in held.items():
                ours[symbol] = ours.get(symbol, 0) + qty
        symbols = sorted(exchange.keys() | ours.keys())
        books = await app.state.redis.mget([f"md:last:{s}" for s in symbols]) if symbols else []
        settled = {}
        for symbol, book in zip(symbols, books, strict=True):
            settlement = (json.loads(book) if book else {}).get("settlement") or {}
            agree = by_trader is not None and exchange.get(symbol, 0) == ours.get(symbol, 0)
            if settlement.get("final") and agree:
                settled[symbol] = settlement["price"]
                exchange.pop(symbol, None)
                for held in by_trader.values():
                    held.pop(symbol, None)
        return {
            "exchange": exchange,
            "settled": settled,
            "dropcopy_connected": dc["connected"],
            "exchange_time": dc["time"],
            "by_trader": by_trader,
        }

    @app.get("/api/risk")
    async def risk(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        """riskmonitor's snapshot, cut down to what `user` may see.

        risk.view.all: every trader. risk.view.own: only their own entry, plus the desk's totals (never
        other traders' rows) if they also have risk.view.desk_aggregate.
        """
        if not (user.can("risk.view.all") or user.can("risk.view.own")):
            raise HTTPException(status_code=403, detail="missing permission risk.view.own")
        raw = await app.state.redis.get(RISK_SNAPSHOT_KEY)
        snap = json.loads(raw) if raw else {"time": "", "traders": {}, "desk": None, "unattributed_fills": 0}
        blocks = decode_blocks(await app.state.redis.hgetall(BLOCKS_KEY))
        for trader in blocks:  # a blocked trader with no positions still gets a row
            zero = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
            snap["traders"].setdefault(trader, {"positions": {}, **zero, "today": zero})
        for trader, entry in snap["traders"].items():
            entry["blocked"] = block_dict(blocks[trader]) if trader in blocks else None
        if user.can("risk.view.all"):
            return snap
        own = snap["traders"].get(user.id)
        desk = snap.get("desk") if user.can("risk.view.desk_aggregate") else None
        traders = {user.id: own} if own else {}
        return {"time": snap["time"], "trading_day": snap.get("trading_day"), "traders": traders, "desk": desk}

    def block_dict(block: TraderBlock) -> dict[str, str]:
        return {"reason": block.reason, "by": block.by, "time": block.time}

    @app.post("/api/risk/blocks/{trader}")
    async def set_block(
        trader: str, body: BlockRequest, user: Annotated[User, Depends(require("killswitch.trip"))]
    ) -> dict[str, Any]:
        """Block one trader (no new orders, live orders cancelled) or lift the block."""
        if body.blocked and not body.reason.strip():
            raise HTTPException(status_code=400, detail="give a reason for blocking")
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        if body.blocked:
            block = TraderBlock(reason=body.reason.strip(), by=user.id, time=now)
            await app.state.redis.hset(BLOCKS_KEY, trader, block.to_json())
        else:
            await app.state.redis.hdel(BLOCKS_KEY, trader)
        alert = {
            "type": "trader_blocked" if body.blocked else "trader_unblocked",
            "trader": trader,
            "reason": body.reason.strip(),
            "by": user.id,
            "time": now,
        }
        await app.state.redis.xadd(ALERTS_STREAM, {"data": json.dumps(alert)}, maxlen=10_000)
        log.warning("%s %s by %s: %s", trader, "blocked" if body.blocked else "unblocked", user.id, body.reason)
        return {"trader": trader, "blocked": body.blocked}

    add_rule_routes(app, settings.db, current_user, require)  # trading rules and their changes

    # ---- WebSockets ------------------------------------------------------------------------

    async def authenticate_ws(ws: WebSocket) -> User | None:
        """First message from the browser must be {"type": "auth", "token": ...}.

        Browsers cannot put an Authorization header on a WebSocket, and a token in the URL would end up
        in access logs, so the token travels as the first message instead.
        """
        await ws.accept()
        try:
            msg = await asyncio.wait_for(ws.receive_json(), WS_AUTH_TIMEOUT_S)
        except TimeoutError, WebSocketDisconnect, ValueError:
            await ws.close(code=WS_UNAUTHORIZED)
            return None
        token = msg.get("token", "") if isinstance(msg, dict) and msg.get("type") == "auth" else ""
        user = auth.authenticate(token) if token else None
        if user is None:
            await ws.close(code=WS_UNAUTHORIZED)
        return user

    async def pump(ws: WebSocket, feed: ClientFeed, msg_type: str) -> None:
        """Send the feed to the browser until it disconnects."""

        async def send() -> None:
            while True:
                for item in (await feed.next_batch()).values():
                    await ws.send_json({"type": msg_type, "data": item})

        async def receive() -> None:
            while True:
                await ws.receive_text()  # nothing to handle yet; this notices the browser leaving

        tasks = {asyncio.create_task(send()), asyncio.create_task(receive())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                task.result()

    @app.websocket("/ws/md")
    async def market_data(ws: WebSocket) -> None:
        """Every symbol's latest book on connect, then each change as it happens."""
        user = await authenticate_ws(ws)
        if user is None:
            return
        if not user.can("md.view"):
            await ws.close(code=WS_FORBIDDEN)
            return
        await ws.send_json({"type": "welcome", "user": user.to_dict()})
        hub: MarketDataHub = app.state.hub
        feed = hub.connect()  # register first, so nothing published from here on is missed
        try:
            for symbol, payload in (await hub.latest()).items():
                feed.put(symbol, payload, replace=False)  # never overwrite a newer live update
            await pump(ws, feed, "book")
        finally:
            hub.disconnect(feed)

    @app.websocket("/ws/orders")
    async def order_updates(ws: WebSocket) -> None:
        """The orders this user may see on connect, then every update to them."""
        user = await authenticate_ws(ws)
        if user is None:
            return
        try:
            scope = order_scope(user)
        except HTTPException:
            await ws.close(code=WS_FORBIDDEN)
            return
        await ws.send_json({"type": "welcome", "user": user.to_dict()})
        hub: OrderHub = app.state.orders
        feed = hub.connect(lambda order: scope is None or order.get("trader") == scope)
        try:
            try:
                for order in await oe.list(scope):
                    feed.put(order["cl_ord_id"], order, replace=False)
            except (OrderEntryUnavailable, OrderEntryError) as exc:
                log.warning("could not load existing orders: %s", exc)
            await pump(ws, feed, "order")
        finally:
            hub.disconnect(feed)

    return app
