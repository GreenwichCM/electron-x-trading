"""The FastAPI application: REST under /api, WebSockets under /ws."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from .hub import MarketDataHub
from .settings import Settings


def create_app(redis: Redis | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass their own `redis` (fakeredis); otherwise REDIS_URL is used."""
    settings = settings or Settings.from_env()

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

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/instruments")
    async def instruments() -> list[dict[str, Any]]:
        found = await app.state.hub.instruments()
        return [found[s] for s in sorted(found)]

    @app.get("/api/md/{symbol}")
    async def latest_book(symbol: str) -> dict[str, Any]:
        book = (await app.state.hub.latest()).get(symbol)
        if book is None:
            raise HTTPException(status_code=404, detail=f"no market data for {symbol}")
        return book

    @app.websocket("/ws/md")
    async def market_data(ws: WebSocket) -> None:
        """Every symbol's latest book on connect, then each change as it happens."""
        await ws.accept()
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
