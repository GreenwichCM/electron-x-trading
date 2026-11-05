"""The FastAPI application: REST under /api, WebSockets under /ws.

No `from __future__ import annotations` here: FastAPI reads the Depends(...) annotations at runtime,
and string annotations could not see the dependency functions defined inside create_app.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from redis.asyncio import Redis

from .auth import Authenticator, DevAuthenticator, User
from .hub import MarketDataHub
from .settings import ConfigError, Settings

WS_AUTH_TIMEOUT_S = 5.0
WS_UNAUTHORIZED = 4401  # application close codes live in 4000-4999
WS_FORBIDDEN = 4403


class DevLoginRequest(BaseModel):
    user_id: str


def create_app(
    redis: Redis | None = None, settings: Settings | None = None, authenticator: Authenticator | None = None
) -> FastAPI:
    """Build the app. Tests pass their own `redis` (fakeredis) and may pass an `authenticator`."""
    settings = settings or Settings.from_env()
    dev = DevAuthenticator(settings.dev_secret) if settings.dev_login else None
    auth = authenticator or dev
    if auth is None:
        raise ConfigError("no login configured: set API_DEV_LOGIN=Y for development")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis if redis is not None else Redis.from_url(settings.redis_url)
        hub = MarketDataHub(client)
        app.state.hub = hub
        task = asyncio.create_task(hub.run(), name="md-hub")
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
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

    # ---- routes ----------------------------------------------------------------------------

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

    @app.websocket("/ws/md")
    async def market_data(ws: WebSocket) -> None:
        """First message from the browser must be {"type": "auth", "token": ...}.

        Browsers cannot put an Authorization header on a WebSocket, and a token in the URL would end up
        in access logs, so the token travels as the first message instead.
        """
        await ws.accept()
        try:
            msg = await asyncio.wait_for(ws.receive_json(), WS_AUTH_TIMEOUT_S)
        except TimeoutError, WebSocketDisconnect, ValueError:
            await ws.close(code=WS_UNAUTHORIZED)
            return
        token = msg.get("token", "") if isinstance(msg, dict) and msg.get("type") == "auth" else ""
        user = auth.authenticate(token) if token else None
        if user is None:
            await ws.close(code=WS_UNAUTHORIZED)
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

            async def send() -> None:
                while True:
                    for payload in (await feed.next_batch()).values():
                        await ws.send_json({"type": "book", "data": payload})

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
        finally:
            hub.disconnect(feed)

    return app
