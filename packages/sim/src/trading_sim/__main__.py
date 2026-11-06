"""Run the simulated ElectronX gateways: `uv run python -m trading_sim`.

Market data on SIM_MD_PORT (13001). Order entry and drop copy share SIM_OE_PORT (13101), as on the
real exchange, and all of them share one order book.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from .gateway import SimGateway
from .marketdata import SimInstrument, SimMarketData
from .orderentry import SimOrderEntry

INSTRUMENTS = [
    SimInstrument("BFUT500-ERCOT-HB_NORTH-261231-15", fair=45.0),
    SimInstrument("BFUT500-ERCOT-HB_WEST-261231-15", fair=38.0),
    SimInstrument("BFUT100-PJM-WESTERN_HUB-261231-15", fair=52.0, high=100.0),
]


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

    md_gateway = SimGateway({md_sender: password})  # market data resets sequence numbers on every logon
    # Order entry and drop copy keep them all week, so the simulator saves them across its own restarts.
    oe_gateway = SimGateway({oe_sender: password, dc_sender: password}, state_path=state_dir / "oe-gateway.json")
    await md_gateway.start(host=host, port=md_port)
    await oe_gateway.start(host=host, port=oe_port)
    md = SimMarketData(md_gateway, INSTRUMENTS)
    SimOrderEntry(oe_gateway, md, drop_copy_senders={dc_sender}, state_path=state_dir / "orders.json")
    log = logging.getLogger("sim")
    log.info("market data: %s / %s on port %d", md_sender, password, md_port)
    log.info("order entry: %s and drop copy: %s, password %s, on port %d", oe_sender, dc_sender, password, oe_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    ticker = asyncio.create_task(md.run(interval))
    await stop.wait()
    ticker.cancel()
    await oe_gateway.stop()
    await md_gateway.stop()


if __name__ == "__main__":
    asyncio.run(main())
