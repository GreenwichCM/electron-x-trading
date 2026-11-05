"""Calls to the orderentry service's internal endpoint (localhost, shared token)."""

from __future__ import annotations

from typing import Any

import httpx2


class OrderEntryUnavailable(Exception):
    """orderentry is not running or not answering."""


class OrderEntryError(Exception):
    """orderentry answered with an error, e.g. 404 unknown order or 409 already closed."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class OrderEntryClient:
    def __init__(self, base_url: str, token: str, transport: httpx2.AsyncBaseTransport | None = None):
        self._http = httpx2.AsyncClient(
            base_url=base_url, headers={"X-Internal-Token": token}, timeout=5.0, transport=transport
        )

    async def submit(self, trader: str, symbol: str, side: str, qty: int, price: float, tif: str) -> dict[str, Any]:
        body = {"trader": trader, "symbol": symbol, "side": side, "qty": qty, "price": price, "tif": tif}
        return await self._request("POST", "/orders", json=body)

    async def cancel(self, trader: str, cl_ord_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/orders/{cl_ord_id}/cancel", json={"trader": trader})

    async def list(self, trader: str | None = None) -> list[dict[str, Any]]:
        return await self._request("GET", "/orders", params={"trader": trader} if trader else None)

    async def positions(self) -> dict[str, dict[str, int]]:
        return await self._request("GET", "/positions")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            raise OrderEntryUnavailable(str(exc) or type(exc).__name__) from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise OrderEntryError(resp.status_code, str(detail))
        return resp.json()
