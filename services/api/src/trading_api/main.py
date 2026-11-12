"""The FastAPI application: REST under /api, WebSockets under /ws.

No `from __future__ import annotations` here: FastAPI reads the Depends(...) annotations at runtime,
and string annotations could not see the dependency functions defined inside create_app.
"""

import asyncio
import contextlib
import json
import logging
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from trading_core import (
    ALERTS_STREAM,
    BLOCKS_KEY,
    KILLSWITCH_KEY,
    KillSwitchState,
    TraderBlock,
    TradingRules,
    decode_blocks,
)
from trading_core.environment import check_redis_async
from trading_db import DbError, WrongEnvironment, connect, follow_rules

from .auth import DEV_USERS, Authenticator, DevAuthenticator, User, users_from_rules
from .hub import ClientFeed, MarketDataHub
from .order_hub import OrderHub
from .orderentry_client import OrderEntryClient, OrderEntryError, OrderEntryUnavailable
from .rules_routes import add_rule_routes
from .settings import ConfigError, Settings

log = logging.getLogger("api")

WS_AUTH_TIMEOUT_S = 5.0
MAX_WATCH = 2000  # symbols one browser may follow at a time
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
    # With a database, the users come from electronx.users (they load with the rules); without one,
    # the built-in test users.
    dev = (
        DevAuthenticator(settings.dev_secret, DEV_USERS if settings.db is None else {}) if settings.dev_login else None
    )
    auth = authenticator or dev
    if auth is None:
        raise ConfigError("no login configured: set API_DEV_LOGIN=Y for development")
    oe = orderentry or OrderEntryClient(settings.oe_url, settings.oe_token)
    record_only = not settings.order_entry  # no orderentry: every order is from the ElectronX screen

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis if redis is not None else Redis.from_url(settings.redis_url)
        await check_redis_async(client, settings.environment)  # WrongEnvironment stops the api here
        if settings.db is not None:
            await asyncio.to_thread(check_database_now, settings)
        app.state.redis = client
        app.state.hub = MarketDataHub(client)
        app.state.orders = OrderHub(client)
        tasks = [
            asyncio.create_task(app.state.hub.run(), name="md-hub"),
            asyncio.create_task(app.state.orders.run(), name="order-hub"),
            asyncio.create_task(app.state.hub.publish_wanted(), name="wanted"),
            asyncio.create_task(app.state.hub.refresh_instruments(), name="instruments"),
            asyncio.create_task(app.state.hub.load_quotes(), name="quotes"),
        ]
        if settings.db is not None:  # the users (for the sign-in and for orders from the ElectronX screen)

            def on_rules(rules: TradingRules) -> None:
                app.state.orders.set_rules(rules)
                if dev is not None:
                    dev.set_users(users_from_rules(rules))

            rules = asyncio.create_task(
                follow_rules(settings.db, on_rules, settings.rules_refresh_s, appname="api"), name="rules"
            )
            rules.add_done_callback(stop_if_wrong_environment)
            tasks.append(rules)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.hub.forget_wanted()
        if orderentry is None:
            await oe.aclose()
        if redis is None:
            await client.aclose()

    app = FastAPI(title="ElectronX trading api", lifespan=lifespan)
    app.state.environment = settings.environment
    app.add_middleware(GZipMiddleware, minimum_size=2000)  # the instrument list shrinks about tenfold

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

    def no_order_entry_here() -> None:
        if record_only:
            raise HTTPException(
                status_code=409, detail="record-only: orders are entered on the ElectronX screen, not from here"
            )

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

    @app.get("/api/environment")
    async def environment() -> dict[str, Any]:
        """Which exchange this is (development = the simulator, beta, production), and whether orders can be
        sent from the app. No sign-in needed: the login page shows it too."""
        return {"name": settings.environment, "order_entry": settings.order_entry}

    if dev is not None:

        @app.get("/api/dev/users")
        async def dev_users() -> list[dict[str, Any]]:
            return [u.to_dict() for u in dev.users.values()]

        @app.post("/api/dev/login")
        async def dev_login(body: DevLoginRequest) -> dict[str, Any]:
            user = dev.users.get(body.user_id)
            if user is None:
                raise HTTPException(status_code=404, detail=f"no active user {body.user_id}")
            return {"token": dev.issue(user.id), "user": user.to_dict()}

    @app.get("/api/me")
    async def me(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        return user.to_dict()

    @app.get("/api/instruments")
    async def instruments(request: Request, user: Annotated[User, Depends(require("md.view"))]) -> Response:
        """Every listed contract: symbol, state and last trade time (thousands on the real exchange).

        Served from memory with an ETag: a browser asking again gets "304 Not Modified", no body, until the
        list changes; the browser then uses its cached copy.
        """
        data, etag = await app.state.hub.instrument_list_now()
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content=data, media_type="application/json", headers=headers)

    @app.get("/api/md/summary")
    async def md_summary(user: Annotated[User, Depends(require("md.view"))]) -> dict[str, Any]:
        """Which contracts have a bid and an offer right now (for "only with prices" on the price screen)."""
        return {"two_sided": sorted(app.state.hub.two_sided)}

    @app.get("/api/md/{symbol}")
    async def latest_book(symbol: str, user: Annotated[User, Depends(require("md.view"))]) -> dict[str, Any]:
        book = (await app.state.hub.latest_for({symbol})).get(symbol)
        if book is None:
            raise HTTPException(status_code=404, detail=f"no market data for {symbol}")
        return book

    @app.get("/api/orders")
    async def list_orders(user: Annotated[User, Depends(current_user)]) -> list[dict[str, Any]]:
        """Orders sent from this app (from orderentry) and orders entered on the ElectronX screen (from
        the drop copy, `source` "electronx"), newest first."""
        scope = order_scope(user)
        ours = [] if record_only else await call_orderentry(oe.list(scope))
        both = [*ours, *app.state.orders.screen_orders(scope)]
        return sorted(both, key=lambda o: o.get("created") or "", reverse=True)

    @app.post("/api/orders")
    async def new_order(
        body: NewOrderRequest, user: Annotated[User, Depends(require("orders.trade"))]
    ) -> dict[str, Any]:
        no_order_entry_here()
        # The trader is always the signed-in user: nobody can place an order in someone else's name.
        return await call_orderentry(oe.submit(user.id, body.symbol, body.side, body.qty, body.price, body.tif))

    @app.post("/api/orders/{cl_ord_id}/cancel")
    async def cancel_order(cl_ord_id: str, user: Annotated[User, Depends(require("orders.trade"))]) -> dict[str, Any]:
        no_order_entry_here()
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

    @app.get("/api/exchange-users/unknown")
    async def unknown_exchange_users(user: Annotated[User, Depends(require("risk.view.all"))]) -> list[dict[str, Any]]:
        """People trading on the ElectronX screen whose ElectronX login is not a user here."""
        return app.state.orders.unknown_users()

    @app.get("/api/positions")
    async def positions(user: Annotated[User, Depends(require("risk.view.all"))]) -> dict[str, Any]:
        """The exchange's positions for the whole account (drop copy) next to orderentry's, per trader.

        `screen` is the part of the exchange's position traded on the ElectronX screen (orders orderentry
        never sent), by contract and by ElectronX user; exchange minus screen should equal orderentry.
        `pending` is a fill dropcopy cannot place yet (for a second or two): neither a match nor a break.
        A contract with a final settlement price is closed, so it is left out (listed under `settled`) as
        long as both sides agree on it; a break stays visible.
        """
        raw = await app.state.redis.get(DC_POSITIONS_KEY)
        dc = json.loads(raw) if raw else {"positions": {}, "connected": False, "time": ""}
        try:
            by_trader = {} if record_only else await oe.positions()  # record-only: nothing is ours
        except OrderEntryUnavailable, OrderEntryError:
            by_trader = None
        exchange = dict(dc["positions"])
        screen = dict(dc.get("screen") or {})
        pending = dict(dc.get("pending") or {})
        screen_by_user = {user: dict(held) for user, held in (dc.get("screen_by_user") or {}).items()}
        ours: dict[str, int] = {}
        for held in (by_trader or {}).values():
            for symbol, qty in held.items():
                ours[symbol] = ours.get(symbol, 0) + qty
        symbols = sorted(exchange.keys() | ours.keys() | screen.keys())
        books = await app.state.redis.mget([f"md:last:{s}" for s in symbols]) if symbols else []
        settled = {}
        for symbol, book in zip(symbols, books, strict=True):
            settlement = (json.loads(book) if book else {}).get("settlement") or {}
            known = exchange.get(symbol, 0) - screen.get(symbol, 0) - pending.get(symbol, 0)
            agree = by_trader is not None and not pending.get(symbol) and known == ours.get(symbol, 0)
            if settlement.get("final") and agree:
                settled[symbol] = settlement["price"]
                for held in (exchange, screen, pending, *screen_by_user.values(), *by_trader.values()):
                    held.pop(symbol, None)
        return {
            "exchange": exchange,
            "screen": screen,
            "screen_by_user": {user: held for user, held in screen_by_user.items() if held},
            "pending": pending,
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
        except WebSocketDisconnect:
            return None  # the browser left before signing in (signing out, leaving the page): nothing to close
        except TimeoutError, ValueError:
            await ws.close(code=WS_UNAUTHORIZED)
            return None
        token = msg.get("token", "") if isinstance(msg, dict) and msg.get("type") == "auth" else ""
        user = auth.authenticate(token) if token else None
        if user is None:
            await ws.close(code=WS_UNAUTHORIZED)
        return user

    async def pump(
        ws: WebSocket, feed: ClientFeed, msg_type: str, on_message: Callable[[dict], Any] | None = None
    ) -> None:
        """Send the feed to the browser until it disconnects; hand what it sends to `on_message`."""

        async def send() -> None:
            while True:
                for item in (await feed.next_batch()).values():
                    await ws.send_json({"type": msg_type, "data": item})

        async def receive() -> None:
            while True:
                text = await ws.receive_text()  # also notices the browser leaving
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if on_message is not None and isinstance(msg, dict):
                    await on_message(msg)

        tasks = {asyncio.create_task(send()), asyncio.create_task(receive())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                task.result()

    @app.websocket("/ws/md")
    async def market_data(ws: WebSocket) -> None:
        """Every symbol's latest book on connect, then each change as it happens.

        With ?watch=1 it sends nothing until the browser says what it shows, {"type": "watch",
        "symbols": [...]}, and then only those: their latest books, then their changes. The browser
        sends a new list whenever its view changes.
        """
        user = await authenticate_ws(ws)
        if user is None:
            return
        if not user.can("md.view"):
            await ws.close(code=WS_FORBIDDEN)
            return
        await ws.send_json({"type": "welcome", "user": user.to_dict()})
        hub: MarketDataHub = app.state.hub
        selective = ws.query_params.get("watch") == "1"
        feed = hub.connect(set() if selective else None)  # register first: nothing published from now is missed

        async def on_message(msg: dict) -> None:
            if msg.get("type") != "watch" or not isinstance(msg.get("symbols"), list):
                return
            new = feed.watch({str(s) for s in msg["symbols"][:MAX_WATCH]})
            for symbol, payload in (await hub.latest_for(new)).items():
                feed.put(symbol, payload, replace=False)  # never overwrite a newer live update

        try:
            if not selective:
                for symbol, payload in (await hub.latest()).items():
                    feed.put(symbol, payload, replace=False)
            await pump(ws, feed, "book", on_message)
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
            for order in hub.screen_orders(scope):
                feed.put(order["cl_ord_id"], order, replace=False)
            try:
                for order in [] if record_only else await oe.list(scope):
                    feed.put(order["cl_ord_id"], order, replace=False)
            except (OrderEntryUnavailable, OrderEntryError) as exc:
                log.warning("could not load existing orders: %s", exc)
            await pump(ws, feed, "order")
        finally:
            hub.disconnect(feed)

    return app


def check_database_now(settings: Settings) -> None:
    """Connecting checks the database belongs to this environment (WrongEnvironment if not). If it cannot
    be reached now, the rules follower checks when it can (stop_if_wrong_environment)."""
    assert settings.db is not None
    try:
        connect(settings.db, appname="api", login_timeout=5).close()
    except DbError as exc:
        log.warning("cannot reach %r yet (%s)", settings.db, exc)


def stop_if_wrong_environment(task: asyncio.Task) -> None:
    if not task.cancelled() and isinstance(task.exception(), WrongEnvironment):
        log.critical("wrong environment, stopping: %s", task.exception())
        signal.raise_signal(signal.SIGTERM)  # uvicorn shuts down; honcho then stops the rest
