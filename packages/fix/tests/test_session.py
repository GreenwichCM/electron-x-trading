"""FixSession against the local simulator gateway, over real TCP on 127.0.0.1."""

import asyncio
import time

from trading_fix import FixSession, SeqStore, SessionConfig
from trading_sim import SimGateway

SENDER = "TEST-OE01"
PASSWORD = "sim"


class RecordingApp:
    def __init__(self):
        self.logons = 0
        self.disconnects = 0
        self.messages = []

    def on_logon(self, session):
        self.logons += 1

    def on_disconnect(self, session):
        self.disconnects += 1

    def on_app_message(self, session, msg):
        self.messages.append(msg)


def config(port, password=PASSWORD, heartbeat_s=30):
    return SessionConfig(
        name="oe",
        host="127.0.0.1",
        port=port,
        sender_comp_id=SENDER,
        target_comp_id="EXI",
        password=password,
        heartbeat_s=heartbeat_s,
    )


async def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 20))


def test_logon_succeeds_with_right_password():
    async def scenario():
        gw = SimGateway({SENDER: PASSWORD})
        port = await gw.start()
        app = RecordingApp()
        session = FixSession(config(port), app, SeqStore())
        task = asyncio.create_task(session.run())
        await wait_for(lambda: session.logged_on)
        assert app.logons == 1
        await session.stop()
        await task
        await gw.stop()

    run(scenario())


def test_logon_rejected_with_wrong_password():
    async def scenario():
        gw = SimGateway({SENDER: PASSWORD})
        port = await gw.start()
        app = RecordingApp()
        session = FixSession(config(port, password="wrong"), app, SeqStore())
        task = asyncio.create_task(session.run())
        await wait_for(lambda: gw.rejected_logons >= 1)
        await asyncio.sleep(0.3)
        assert not session.logged_on and app.logons == 0
        await session.stop()
        await task
        await gw.stop()

    run(scenario())


def test_only_one_connection_per_sender_comp_id():
    async def scenario():
        gw = SimGateway({SENDER: PASSWORD})
        port = await gw.start()
        first = FixSession(config(port), RecordingApp(), SeqStore())
        t1 = asyncio.create_task(first.run())
        await wait_for(lambda: first.logged_on)
        second_app = RecordingApp()
        second = FixSession(config(port), second_app, SeqStore())
        t2 = asyncio.create_task(second.run())
        await wait_for(lambda: gw.rejected_logons >= 1)
        assert first.logged_on and second_app.logons == 0
        await second.stop()
        await first.stop()
        await asyncio.gather(t1, t2)
        await gw.stop()

    run(scenario())


def test_heartbeats_keep_flowing():
    async def scenario():
        gw = SimGateway({SENDER: PASSWORD})
        port = await gw.start()
        session = FixSession(config(port, heartbeat_s=1), RecordingApp(), SeqStore())
        task = asyncio.create_task(session.run())
        await wait_for(lambda: session.logged_on)
        await asyncio.sleep(2.5)
        assert gw.session(SENDER).received_types["0"] >= 2
        assert session.logged_on
        await session.stop()
        await task
        await gw.stop()

    run(scenario())


def test_reconnect_keeps_sequence_numbers_and_recovers_missed_messages():
    async def scenario():
        gw = SimGateway({SENDER: PASSWORD})
        port = await gw.start()
        app = RecordingApp()
        session = FixSession(config(port), app, SeqStore())
        task = asyncio.create_task(session.run())
        await wait_for(lambda: session.logged_on)

        gw.drop(SENDER)  # network failure: no Logout
        await wait_for(lambda: app.disconnects == 1)
        gw.send_app(SENDER, "8", [(11, "MISSED-1")])  # sent while the client is away
        gw.send_app(SENDER, "8", [(11, "MISSED-2")])

        await wait_for(lambda: app.logons == 2)
        await wait_for(lambda: len(app.messages) == 2)
        assert [m.get(11) for m in app.messages] == ["MISSED-1", "MISSED-2"]
        assert all(m.get(43) == "Y" for m in app.messages)  # marked as possible duplicates

        first, second = gw.session(SENDER).logon_seqs
        assert first == 1 and second > first  # carried on, not reset to 1
        await session.stop()
        await task
        await gw.stop()

    run(scenario())


def test_sequence_numbers_survive_a_process_restart(tmp_path):
    async def scenario():
        gw = SimGateway({SENDER: PASSWORD})
        port = await gw.start()
        path = tmp_path / "oe.json"

        session = FixSession(config(port), RecordingApp(), SeqStore(path))
        task = asyncio.create_task(session.run())
        await wait_for(lambda: session.logged_on)
        await session.stop()
        await task

        store = SeqStore(path)  # a new process would build the store from the same file
        next_seq = store.next_sender
        assert next_seq > 1
        restarted = FixSession(config(port), RecordingApp(), store)
        task = asyncio.create_task(restarted.run())
        await wait_for(lambda: restarted.logged_on)
        assert gw.session(SENDER).logon_seqs[-1] == next_seq
        await restarted.stop()
        await task
        await gw.stop()

    run(scenario())
