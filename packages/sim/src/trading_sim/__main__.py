"""Run the simulated ElectronX gateways: `uv run python -m trading_sim`.

Market data on SIM_MD_PORT (13001). Order entry and drop copy share SIM_OE_PORT (13101), as on the
real exchange, and all of them share one order book.

SIM_UNIVERSE=full lists what the exchange does: every hour of today and tomorrow for each ERCOT hub
and PJM Western Hub, in BFUT100, BFUT500, BFUT5000 and BOPT (about 1,800 contracts), rolling on as
days go by. Without it there are three contracts that never expire.

SIM_EXPIRING_IN_S=300 adds an ERCOT Houston contract that stops trading 5 minutes after the start and
settles SIM_SETTLE_AFTER_S (30) seconds later, to try out expiry and settlement.

SIM_TLS_CERT, SIM_TLS_KEY and SIM_TLS_CA make both gateways require mutual TLS like the real ones: the
simulator's own certificate and key, and the CA that client certificates must be signed by.

SIM_SCREEN_USERS (comma-separated ElectronX logins) has those traders place a small order on the
"ElectronX screen" every SIM_SCREEN_EVERY_S (60) seconds: no FIX session sends it, only the drop copy
reports it, as for orders traders enter on the exchange's own screen.

Writing `last` (or a TrdMatchID) to the file `bust` in SIM_STATE_DIR busts that trade, as ElectronX's
operations team can: `echo last > var/sim/bust`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import ssl
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .gateway import SimGateway
from .marketdata import BID, BUY, OFFER, SELL, SimInstrument, SimMarketData
from .orderentry import SimOrderEntry
from .universe import full_universe, roll

INSTRUMENTS = [
    SimInstrument("BFUT500-ERCOT-HB_NORTH-261231-15", fair=45.0),
    SimInstrument("BFUT500-ERCOT-HB_WEST-261231-15", fair=38.0),
    SimInstrument("BFUT100-PJM-WESTERN_HUB-261231-15", fair=52.0, high=100.0),
]

log = logging.getLogger("sim")


def expiring_instrument(in_s: float, settle_after_s: float) -> SimInstrument:
    """A contract whose trading ends `in_s` seconds from now, named for the ERCOT hour it ends in."""
    ends = time.time() + in_s
    local = datetime.fromtimestamp(ends, ZoneInfo("America/Chicago"))
    symbol = f"BFUT500-ERCOT-HB_HOUSTON-{local:%y%m%d}-{local.hour + 1:02d}"
    return SimInstrument(symbol, fair=41.0, last_trade_at=ends, settle_after_s=settle_after_s)


def tls_context() -> ssl.SSLContext | None:
    """Mutual TLS, if SIM_TLS_CERT is set: clients must present a certificate signed by SIM_TLS_CA."""
    cert = os.environ.get("SIM_TLS_CERT", "").strip()
    if not cert:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=os.environ["SIM_TLS_CA"])
    ctx.load_cert_chain(cert, os.environ["SIM_TLS_KEY"])
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def keep_rolling(md: SimMarketData, rng: random.Random, every_s: float = 60.0) -> None:
    while True:
        await asyncio.sleep(every_s)
        roll(md, rng)


async def screen_traders(oe: SimOrderEntry, users: list[str], every_s: float, rng: random.Random) -> None:
    """Now and then one of `users` takes a lot on the ElectronX screen (an IOC order that crosses)."""
    while True:
        await asyncio.sleep(every_s)
        md = oe.md
        open_ = [s for s, i in md.instruments.items() if i.state == "OPEN" and md.best(s, BID) and md.best(s, OFFER)]
        if not open_:
            continue
        symbol, user, side = rng.choice(open_), rng.choice(users), rng.choice((BUY, SELL))
        price = md.best(symbol, OFFER if side == BUY else BID)
        order = oe.screen_order(user, symbol, side, 1, price, tif="3")
        log.info(
            "%s on the screen: %s 1 %s @ %.2f (%s)", user, "BUY" if side == BUY else "SELL", symbol, price, order.status
        )


async def bust_on_request(oe: SimOrderEntry, path: Path, every_s: float = 1.0) -> None:
    """Bust the trade named in `path` (`last` for the latest), then remove the file."""
    while True:
        await asyncio.sleep(every_s)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        path.unlink(missing_ok=True)
        sent = oe.bust("" if text in ("", "last") else text)
        if sent:
            log.warning("busted trade %s: %d report(s) sent", text or "last", sent)
        else:
            log.warning("no trade %s to bust (only trades since the simulator started can be)", text or "last")


async def main() -> None:
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format=log_format)
    host = os.environ.get("SIM_HOST", "127.0.0.1")
    md_port = int(os.environ.get("SIM_MD_PORT", "13001"))
    oe_port = int(os.environ.get("SIM_OE_PORT", "13101"))
    md_sender = os.environ.get("SIM_MD_SENDER", "HURON-MD01")
    oe_sender = os.environ.get("SIM_OE_SENDER", "D-HURON-01-OE01")
    dc_sender = os.environ.get("SIM_DC_SENDER", "CM-HURON-DC01")
    password = os.environ.get("SIM_PASSWORD", "sim")
    interval = float(os.environ.get("SIM_INTERVAL_S", "0.5"))
    state_dir = Path(os.environ.get("SIM_STATE_DIR", "var/sim"))
    full = os.environ.get("SIM_UNIVERSE", "").strip().lower() == "full"
    rng = random.Random()
    instruments = full_universe(time.time(), rng) if full else list(INSTRUMENTS)
    if os.environ.get("SIM_EXPIRING_IN_S", "").strip():
        settle_after = float(os.environ.get("SIM_SETTLE_AFTER_S", "30"))
        instruments.append(expiring_instrument(float(os.environ["SIM_EXPIRING_IN_S"]), settle_after))

    md_gateway = SimGateway({md_sender: password})  # market data resets sequence numbers on every logon
    # Order entry and drop copy keep them all week, so the simulator saves them across its own restarts.
    oe_gateway = SimGateway({oe_sender: password, dc_sender: password}, state_path=state_dir / "oe-gateway.json")
    tls = tls_context()
    await md_gateway.start(host=host, port=md_port, ssl_context=tls)
    await oe_gateway.start(host=host, port=oe_port, ssl_context=tls)
    md = SimMarketData(md_gateway, instruments)
    if full:
        md.active_per_step = int(os.environ.get("SIM_ACTIVE_PER_STEP", "40"))
    oe = SimOrderEntry(oe_gateway, md, drop_copy_senders={dc_sender}, state_path=state_dir / "orders.json")
    log.info("market data: %s / %s on port %d", md_sender, password, md_port)
    log.info("order entry: %s and drop copy: %s, password %s, on port %d", oe_sender, dc_sender, password, oe_port)
    log.info("%d contracts listed%s", len(instruments), " (full universe, rolling)" if full else "")
    if tls is not None:
        log.info("mutual TLS required on both ports")
    for inst in instruments:
        if inst.last_trade_at is not None and not full:
            ends = datetime.fromtimestamp(inst.last_trade_at).astimezone().isoformat(timespec="seconds")
            log.info("%s stops trading at %s", inst.symbol, ends)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(md.run(interval)), asyncio.create_task(bust_on_request(oe, state_dir / "bust"))]
    if full:
        tasks.append(asyncio.create_task(keep_rolling(md, rng)))
    users = [u.strip() for u in os.environ.get("SIM_SCREEN_USERS", "").split(",") if u.strip()]
    if users:
        every = float(os.environ.get("SIM_SCREEN_EVERY_S", "60"))
        log.info("screen orders from %s every %.0fs", ", ".join(users), every)
        tasks.append(asyncio.create_task(screen_traders(oe, users, every, rng)))
    await stop.wait()
    for task in tasks:
        task.cancel()
    await oe_gateway.stop()
    await md_gateway.stop()


if __name__ == "__main__":
    asyncio.run(main())
