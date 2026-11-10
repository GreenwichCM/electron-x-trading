"""The Snapshot API client, and how its marks and settlements reach the published books.

The sample answers are cut down from real beta answers (2026-11-08)."""

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from trading_marketdata import MarketDataApp, MemoryPublisher, OrderBook
from trading_marketdata.snapshot import SnapshotClient, parse_marks, parse_settlements, poll_once

FUT = "BFUT100-ERCOT-HB_PAN-261109-13"
OPT = "BOPT-ERCOT-HB_HOUSTON-261108-14"
OPT_MARKED = "BOPT-ERCOT-HB_PILOT-261108-15"
SETTLED = "BFUT500-ERCOT-HB_PILOT-261108-20"
PJM = "BFUT100-PJM-WESTERN_HUB-261231-15"

MARKS = {
    "channel": "marks",
    "snapshots": [
        {
            "channel": "marks",
            "timestamp": "2026-11-08T18:34:37.017491349Z",
            "events": [
                {
                    "marks": {"dam": {"ts": "2026-11-08T18:34:36.999807Z", "value": 3106}},
                    "type": "marks",
                    "symbol": FUT,
                }
            ],
        },
        {
            "channel": "marks",
            "timestamp": "2026-11-08T15:13:41.127383173Z",
            "events": [
                {
                    "marks": {"dam": {"ts": "2026-11-08T15:13:22.354849Z", "value": 2784}},
                    "type": "marks",
                    "symbol": OPT_MARKED,
                }
            ],
        },
    ],
}

SETTLEMENT = {
    "channel": "settlement",
    "snapshots": [
        {
            "channel": "settlement",
            "timestamp": "2026-11-09T02:04:48.280216173Z",
            "events": [
                {
                    "type": "snapshot",
                    "symbol": SETTLED,
                    "iso": "ERCOT",
                    "hub": "HB_PILOT",
                    "delivery_date": "2026-11-08",
                    "hour_ending": "20",
                    "repeated_hour": "false",
                    "raw_settlement_price": "99.9325",
                    "payout_price": "99.93",
                }
            ],
        },
        {
            "channel": "settlement",
            "timestamp": "2026-11-08T20:05:08.865003977Z",
            "events": [
                {
                    "type": "snapshot",
                    "symbol": OPT,
                    "iso": "ERCOT",
                    "hub": "HB_HOUSTON",
                    "delivery_date": "2026-11-08",
                    "hour_ending": "14",
                    "repeated_hour": "false",
                    "raw_settlement_price": "49.1925",
                    "raw_dayahead_price": "28.73",
                    "payout_price": "100",
                }
            ],
        },
    ],
}


def test_marks_are_in_cents():
    marks = parse_marks(MARKS)
    assert marks[FUT] == {"dam": {"price": 31.06, "time": "2026-11-08T18:34:36.999+00:00"}}
    assert marks[OPT_MARKED]["dam"]["price"] == 27.84  # for a BOPT, its strike


def test_settlement_is_the_payout_price():
    settled = parse_settlements(SETTLEMENT)
    assert settled[SETTLED] == {"price": 99.93, "raw": 99.9325, "time": "2026-11-09T02:04:48.280+00:00"}
    assert settled[OPT]["price"] == 100.0  # the option paid out


def test_broken_answers_are_skipped():
    assert parse_marks({"snapshots": [{"events": [{"symbol": FUT, "marks": {"dam": {"value": "x"}}}, {}]}]}) == {}
    assert parse_settlements(None) == {} and parse_marks({"snapshots": None}) == {}


def app_following(*symbols):
    pub = MemoryPublisher()
    app = MarketDataApp(pub)
    for symbol in symbols:
        app.books[symbol] = OrderBook(symbol)
    return pub, app


def fake_api(seen):
    async def fetch(url):
        query = parse_qs(urlsplit(url).query)
        seen.append(query)
        return {"marks": MARKS, "settlement": SETTLEMENT}[query["channel"][0]]

    return fetch


def test_marks_and_settlements_reach_the_books_of_contracts_followed():
    pub, app = app_following(FUT, SETTLED, PJM)
    seen = []
    client = SnapshotClient("https://beta.example/", "k3y", fetch=fake_api(seen))
    assert asyncio.run(poll_once(client, app)) == (2, 2)
    assert [q["product_ids"] for q in seen] == [["ERCOT,PJM"], ["ERCOT,PJM"]]  # the ISOs followed
    assert all(q["api_key"] == ["k3y"] for q in seen)
    assert pub.books[FUT]["marks"]["dam"]["price"] == 31.06
    assert pub.books[SETTLED]["settlement"] == {
        "price": 99.93,
        "final": True,
        "flag": "",
        "source": "snapshot",
        "time": "2026-11-09T02:04:48.280+00:00",
    }
    assert OPT not in pub.books  # not followed here

    updates = len(pub.book_updates)
    asyncio.run(poll_once(client, app))
    assert len(pub.book_updates) == updates  # nothing changed, nothing published


def test_fix_settlement_comes_first_and_a_difference_is_reported(caplog):
    _, app = app_following(SETTLED)
    app._settlement(SETTLED, {270: "99.93", 272: "20261109", 273: "02:00:00.000"})
    app.on_settlements(parse_settlements(SETTLEMENT))
    assert app.settlements[SETTLED]["source"] == "fix"  # the same price: FIX had it first

    _, app = app_following(SETTLED)
    app.on_settlements(parse_settlements(SETTLEMENT))
    app._settlement(SETTLED, {270: "99.9325"})  # unrounded: the same within a cent, so it is kept
    assert app.settlements[SETTLED]["source"] == "snapshot"
    app._settlement(SETTLED, {270: "98.00", 286: "3"})  # an indicative price after the final one: ignored
    assert app.settlements[SETTLED]["price"] == 99.93
    with caplog.at_level(logging.WARNING):
        app._settlement(SETTLED, {270: "98.00"})
    assert app.settlements[SETTLED] | {"time": ""} == {
        "price": 98.0,
        "final": True,
        "flag": "",
        "source": "fix",
        "time": "",
    }
    assert "settlement differs" in caplog.text


def test_failures_are_logged_without_the_key(caplog):
    async def broken(url):
        raise RuntimeError(f"could not reach {url}")

    _, app = app_following(FUT)
    client = SnapshotClient("https://beta.example", "s3cret-key", fetch=broken)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(poll_once(client, app)) is None
    assert "Snapshot API request failed" in caplog.text and "s3cret-key" not in caplog.text


def test_nothing_is_asked_before_any_contract_is_followed():
    seen = []
    _, app = app_following()
    assert asyncio.run(poll_once(SnapshotClient("https://x", "k", fetch=fake_api(seen)), app)) is None
    assert seen == []


def test_real_http_request():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, body = (401, {"error": "bad key"}) if "wrong" in self.path else (200, MARKS)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        body = asyncio.run(SnapshotClient(base, "k3y").get("marks", ["ERCOT", "PJM"]))
        assert parse_marks(body)[FUT]["dam"]["price"] == 31.06
        assert requests == ["/api/v1/mkdata/snapshot?channel=marks&product_ids=ERCOT%2CPJM&api_key=k3y"]

        _, app = app_following(FUT)
        assert asyncio.run(poll_once(SnapshotClient(base, "wrong"), app)) is None
    finally:
        server.shutdown()
        server.server_close()
