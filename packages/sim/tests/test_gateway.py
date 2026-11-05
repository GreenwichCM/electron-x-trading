"""Sequence number recovery between a FixSession and the simulated gateway."""

import asyncio
import time

from trading_fix import FixSession, SeqStore, SessionConfig
from trading_sim import SimGateway

SENDER = "TEST-OE01"


class Recorder:
    def __init__(self):
        self.messages = []

    def on_logon(self, session):
        pass

    def on_disconnect(self, session):
        pass

    def on_app_message(self, session, msg):
        self.messages.append(msg)


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 20))


def cfg(port):
    return SessionConfig(
        name="oe", host="127.0.0.1", port=port, sender_comp_id=SENDER, target_comp_id="EXI", password="sim"
    )


def test_simulator_restart_keeps_sequence_numbers(tmp_path):
    async def scenario():
        state = tmp_path / "oe-gateway.json"
        gw = SimGateway({SENDER: "sim"}, state_path=state)
        port = await gw.start()
        app = Recorder()
        session = FixSession(cfg(port), app, SeqStore())
        task = asyncio.create_task(session.run())
        await wait_for(lambda: session.logged_on)
        gw.send_app(SENDER, "8", [(11, "BEFORE")])
        await wait_for(lambda: len(app.messages) == 1)

        await gw.stop()  # the simulator restarts on the same port
        restarted = SimGateway({SENDER: "sim"}, state_path=state)
        await restarted.start(port=port)
        await wait_for(lambda: not session.logged_on)
        await wait_for(lambda: session.logged_on, timeout=8)
        restarted.send_app(SENDER, "8", [(11, "AFTER")])
        await wait_for(lambda: [m.get(11) for m in app.messages] == ["BEFORE", "AFTER"])
        await session.stop()
        await task
        await restarted.stop()

    run(scenario())


def test_both_sides_missing_messages_still_recover():
    """The gateway lost what we sent and we lost what it sent: each side's ResendRequest must be answered."""

    async def scenario():
        gw = SimGateway({SENDER: "sim"})
        port = await gw.start()
        app = Recorder()
        session = FixSession(cfg(port), app, SeqStore())
        task = asyncio.create_task(session.run())
        await wait_for(lambda: session.logged_on)
        gw.drop(SENDER)
        await wait_for(lambda: not session.logged_on)
        s = gw.session(SENDER)
        s.in_expected = 1  # forgot our messages
        s.out_seq += 3  # sent three we never received (and kept no copy)
        await wait_for(lambda: session.logged_on, timeout=8)
        gw.send_app(SENDER, "8", [(11, "LIVE")])
        await wait_for(lambda: [m.get(11) for m in app.messages] == ["LIVE"])
        await session.stop()
        await task
        await gw.stop()

    run(scenario())
