"""Run the simulated ElectronX market data gateway: `uv run python -m trading_sim`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .gateway import SimGateway
from .marketdata import SimInstrument, SimMarketData

INSTRUMENTS = [
    SimInstrument("BFUT500-ERCOT-HB_NORTH-261231-15", fair=45.0),
    SimInstrument("BFUT500-ERCOT-HB_WEST-261231-15", fair=38.0),
    SimInstrument("BFUT100-PJM-WESTERN_HUB-261231-15", fair=52.0, high=100.0),
]


async def main() -> None:
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format=log_format)
    port = int(os.environ.get("SIM_MD_PORT", "13001"))
    sender = os.environ.get("SIM_MD_SENDER", "HURON-MD01")
    password = os.environ.get("SIM_MD_PASSWORD", "sim")
    interval = float(os.environ.get("SIM_INTERVAL_S", "0.5"))

    gateway = SimGateway({sender: password})
    await gateway.start(host=os.environ.get("SIM_HOST", "127.0.0.1"), port=port)
    md = SimMarketData(gateway, INSTRUMENTS)
    logging.getLogger("sim").info("market data gateway ready: %s / %s on port %d", sender, password, port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    ticker = asyncio.create_task(md.run(interval))
    await stop.wait()
    ticker.cancel()
    await gateway.stop()


if __name__ == "__main__":
    asyncio.run(main())
