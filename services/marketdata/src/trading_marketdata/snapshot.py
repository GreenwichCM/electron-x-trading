"""ElectronX Snapshot API (REST): the exchange's own marks and settlement prices, next to FIX market data.

    GET <SNAPSHOT_URL>/api/v1/mkdata/snapshot?channel=<channel>&product_ids=<ISO,...>&api_key=<key>

Both channels answer {"snapshots": [{"timestamp": ..., "events": [{"symbol": ..., ...}]}, ...]}:
  * `marks`: the exchange's marks per contract, by kind. So far only `dam`, the day-ahead price, in cents:
    {"marks": {"dam": {"ts": "2026-11-08T15:13:22.354849Z", "value": 2784}}} is 27.84 USD/MWh.
    For a BOPT the day-ahead price is its strike, not a price of the option.
  * `settlement`: one entry per contract once it has settled. `payout_price` is what the contract pays
    (for a BFUT the settlement price to the cent, for a BOPT 0 or 100); `raw_settlement_price` is the
    ISO's price and `raw_dayahead_price` (BOPT only) the strike.

The API takes the key only in the query string, so URLs are never logged and error messages have the
key cut out.

Check the connection: `uv run --env-file services/marketdata/.env python -m trading_marketdata.snapshot ERCOT`
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

if TYPE_CHECKING:
    from .app import MarketDataApp

log = logging.getLogger("md.snapshot")

PATH = "/api/v1/mkdata/snapshot"
CENTS = 100.0  # marks come in cents

Fetch = Callable[[str], Awaitable[Any]]
Marks = dict[str, dict[str, dict[str, Any]]]  # symbol -> kind ("dam") -> {"price", "time"}
Settlements = dict[str, dict[str, Any]]  # symbol -> {"price", "raw", "time"}


def _iso(value: Any) -> str | None:
    """An API timestamp (UTC, up to nanoseconds) as ISO 8601 to the millisecond."""
    try:
        return datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="milliseconds")
    except TypeError, ValueError:
        return None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _events(body: Any) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """(snapshot, event) for every event in an answer."""
    for snap in (body or {}).get("snapshots") or []:
        for event in snap.get("events") or []:
            if isinstance(event, dict) and event.get("symbol"):
                yield snap, event


def parse_marks(body: Any) -> Marks:
    out: Marks = {}
    for snap, event in _events(body):
        for kind, mark in (event.get("marks") or {}).items():
            value = _num((mark or {}).get("value"))
            if value is not None:
                when = _iso(mark.get("ts")) or _iso(snap.get("timestamp"))
                out.setdefault(event["symbol"], {})[kind] = {"price": round(value / CENTS, 4), "time": when}
    return out


def parse_settlements(body: Any) -> Settlements:
    out: Settlements = {}
    for snap, event in _events(body):
        raw = _num(event.get("raw_settlement_price"))
        payout = _num(event.get("payout_price"))
        price = payout if payout is not None else raw
        if price is not None:
            out[event["symbol"]] = {"price": price, "raw": raw, "time": _iso(snap.get("timestamp"))}
    return out


def _get_json(url: str, timeout_s: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "trading-marketdata"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


class SnapshotClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 10.0, fetch: Fetch | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._fetch = fetch or (lambda url: asyncio.to_thread(_get_json, url, timeout_s))

    async def get(self, channel: str, products: Iterable[str]) -> Any:
        query = urlencode({"channel": channel, "product_ids": ",".join(products), "api_key": self.api_key})
        return await self._fetch(f"{self.base_url}{PATH}?{query}")

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text


async def poll_once(client: SnapshotClient, app: MarketDataApp) -> tuple[int, int] | None:
    """Fetch both channels for the markets followed and hand them to the app.

    Returns how many contracts had marks and how many had settled, or None if there was nothing to ask
    for or a request failed.
    """
    markets = app.markets()
    if not markets:
        return None
    try:
        marks = parse_marks(await client.get("marks", markets))
        settlements = parse_settlements(await client.get("settlement", markets))
    except Exception as exc:  # network, HTTP status, bad JSON: try again next time
        log.warning("Snapshot API request failed: %s: %s", type(exc).__name__, client.redact(str(exc)))
        return None
    app.on_marks(marks)
    app.on_settlements(settlements)
    return len(marks), len(settlements)


async def poll_snapshots(client: SnapshotClient, app: MarketDataApp, every_s: float) -> None:
    """Poll for as long as the service runs. Reports the first answer, then stays quiet unless it fails."""
    answered = False
    while True:
        counts = await poll_once(client, app)
        if counts is not None and not answered:
            answered = True
            log.info(
                "Snapshot API answered for %s: %d contracts with marks, %d settled; %d followed here have marks",
                ",".join(app.markets()),
                *counts,
                len(app.snapshot_marks),
            )
        await asyncio.sleep(every_s)


def main(markets: list[str]) -> None:
    """Print what the Snapshot API sends for the given ISOs, without showing the key."""
    url, key = os.environ.get("SNAPSHOT_URL", "").strip(), os.environ.get("SNAPSHOT_API_KEY", "").strip()
    if not url or not key:
        sys.exit("set SNAPSHOT_URL and SNAPSHOT_API_KEY in services/marketdata/.env")
    client = SnapshotClient(url, key)
    markets = markets or ["ERCOT"]

    async def fetch() -> tuple[Any, Any]:
        return await client.get("marks", markets), await client.get("settlement", markets)

    try:
        marks_body, settlement_body = asyncio.run(fetch())
    except Exception as exc:
        sys.exit(f"request failed: {type(exc).__name__}: {client.redact(str(exc))}")
    marks, settled = parse_marks(marks_body), parse_settlements(settlement_body)
    print(f"{url} {','.join(markets)}: {len(marks)} contracts with marks, {len(settled)} settled")
    for symbol in sorted(marks)[-3:]:
        print("  mark", symbol, marks[symbol])
    for symbol in sorted(settled)[-3:]:
        print("  settlement", symbol, settled[symbol])
    # A BOPT's settlement repeats its strike, the day-ahead price, in USD: it should equal the dam mark.
    strikes = {e["symbol"]: _num(e.get("raw_dayahead_price")) for _, e in _events(settlement_body)}
    both = [s for s in sorted(strikes) if strikes[s] is not None and "dam" in marks.get(s, {})][-5:]
    for symbol in both:
        print(f"  check {symbol}: dam mark {marks[symbol]['dam']['price']}, settlement day-ahead {strikes[symbol]}")
    if not both:
        print("  no BOPT has both a day-ahead mark and a settlement, so the cents check could not run")


if __name__ == "__main__":
    main(sys.argv[1:])
