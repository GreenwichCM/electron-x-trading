"""Session layer of an ElectronX gateway, as the exchange side (the "acceptor").

Reproduces the rules the real gateways enforce at logon and on sequence numbers:
  * Logon needs the right password and DefaultApplVerID(1137)=9, and must not carry SenderSubID(50)
  * one connection per SenderCompID
  * sequence numbers persist across reconnects (unless the Logon carries ResetSeqNumFlag(141)=Y)
  * MsgSeqNum too low -> Logout "MsgSeqNum too low, expecting N"
  * ResendRequest -> application messages replayed with PossDupFlag, admin messages gap-filled
  * a Heartbeat whenever nothing has been sent for HeartBtInt

Order matching and market data are added on top of this in later steps.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable

from trading_fix import FixMessage, FixParseError, FixParser, encode, utc_timestamp

log = logging.getLogger("sim")

BEGIN = "FIXT.1.1"
EXCHANGE_COMP_ID = "EXI"
ADMIN = frozenset({"0", "1", "2", "3", "4", "5", "A"})  # gap-filled on resend, never replayed
Fields = list[tuple[int, str]]


class SimSession:
    """State for one SenderCompID. Outlives TCP connections, like on the real gateway."""

    def __init__(self, sender: str):
        self.sender = sender
        self.out_seq = 1
        self.in_expected = 1
        self.history: dict[int, tuple[str, Fields]] = {}
        self.conn: SimConnection | None = None
        self.received: list[FixMessage] = []  # application messages from the client, for tests
        self.received_types: Counter[str] = Counter()  # every message type from the client, for tests
        self.logon_seqs: list[int] = []  # MsgSeqNum of each accepted Logon, for tests

    def reset(self) -> None:
        self.out_seq = self.in_expected = 1
        self.history.clear()

    def send(self, msg_type: str, body: Fields) -> int:
        """Send now if connected; application messages are kept for ResendRequest either way."""
        seq = self.out_seq
        self.out_seq += 1
        if msg_type not in ADMIN:
            self.history[seq] = (msg_type, body)
        if self.conn is not None:
            self.conn.write(msg_type, body, seq)
        return seq


class SimConnection:
    def __init__(self, gateway: SimGateway, writer: asyncio.StreamWriter):
        self.gateway = gateway
        self.writer = writer
        self.session: SimSession | None = None
        self.last_sent = time.monotonic()
        self.heartbeat_s = 30
        self.logout_sent = False

    def write(self, msg_type: str, body: Fields, seq: int, poss_dup: bool = False, sender: str = "") -> None:
        now = utc_timestamp()
        target = sender or (self.session.sender if self.session else "")
        fields: Fields = [(35, msg_type), (34, str(seq)), (49, EXCHANGE_COMP_ID), (52, now), (56, target)]
        if poss_dup:
            fields += [(43, "Y"), (122, now)]
        fields += body
        self.writer.write(encode(BEGIN, fields))
        self.last_sent = time.monotonic()

    def reject_logon(self, sender: str, text: str) -> None:
        """Refuse a Logon without touching that SenderCompID's real session state."""
        log.info("rejecting logon from %s: %s", sender, text)
        self.write("5", [(58, text)], seq=1, sender=sender)
        self.gateway.rejected_logons += 1
        self.writer.close()

    def close(self) -> None:
        self.writer.close()


class SimGateway:
    """An acceptor for one gateway port. `passwords` maps SenderCompID -> password."""

    def __init__(self, passwords: dict[str, str]):
        self.passwords = passwords
        self.sessions: dict[str, SimSession] = {}
        self.rejected_logons = 0
        self.on_app_message: Callable[[SimSession, FixMessage], None] | None = None
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Start listening. Port 0 picks a free port; the chosen one is returned."""
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("sim gateway listening on %s:%d", host, self.port)
        return self.port

    async def stop(self) -> None:
        for s in self.sessions.values():
            if s.conn is not None:
                s.conn.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def session(self, sender: str) -> SimSession:
        return self.sessions.setdefault(sender, SimSession(sender))

    def send_app(self, sender: str, msg_type: str, body: Fields) -> int:
        """Send an application message to a client (queued for resend if it is disconnected)."""
        return self.session(sender).send(msg_type, body)

    def drop(self, sender: str) -> None:
        """Cut the client's TCP connection without a Logout, like a network failure."""
        s = self.sessions.get(sender)
        if s is not None and s.conn is not None:
            s.conn.writer.transport.abort()

    # ---- connection handling ---------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = SimConnection(self, writer)
        parser = FixParser()
        heartbeat = asyncio.create_task(self._heartbeat_loop(conn))
        try:
            while data := await reader.read(65536):
                for msg in parser.feed(data):
                    self._on_message(conn, msg)
        except (ConnectionError, FixParseError) as exc:
            log.info("connection ended: %s", exc)
        finally:
            heartbeat.cancel()
            if conn.session is not None and conn.session.conn is conn:
                conn.session.conn = None
            conn.close()

    async def _heartbeat_loop(self, conn: SimConnection) -> None:
        while True:
            await asyncio.sleep(0.25)
            s = conn.session
            if s is not None and s.conn is conn and time.monotonic() - conn.last_sent >= conn.heartbeat_s:
                s.send("0", [])

    def _on_message(self, conn: SimConnection, msg: FixMessage) -> None:
        mt = msg.msg_type
        sender = msg.get(49, "")
        if conn.session is None:
            if mt != "A":
                conn.reject_logon(sender, "First message must be Logon")
                return
            self._on_logon(conn, msg)
            return

        s = conn.session
        s.received_types[mt] += 1
        seq = msg.get_int(34)
        if mt == "4" and msg.get(123) == "Y":  # SequenceReset-GapFill
            s.in_expected = msg.get_int(36)
            return
        if seq < s.in_expected:
            if msg.get(43) == "Y":
                return
            self._logout(conn, f"MsgSeqNum too low, expecting {s.in_expected} but received {seq}")
            return
        if seq > s.in_expected:
            s.send("2", [(7, str(s.in_expected)), (16, "0")])
            return
        s.in_expected += 1

        if mt == "0":
            pass
        elif mt == "1":
            s.send("0", [(112, msg.get(112, ""))])
        elif mt == "2":
            self._resend(conn, msg.get_int(7), msg.get_int(16))
        elif mt == "5":
            if not conn.logout_sent:
                conn.logout_sent = True
                s.send("5", [])
            conn.close()
        else:
            s.received.append(msg)
            if self.on_app_message is not None:
                self.on_app_message(s, msg)

    def _on_logon(self, conn: SimConnection, msg: FixMessage) -> None:
        sender = msg.get(49, "")
        if self.passwords.get(sender) is None or msg.get(554) != self.passwords[sender]:
            conn.reject_logon(sender, "Invalid username or password")
            return
        if msg.get(1137) != "9":
            conn.reject_logon(sender, "DefaultApplVerID(1137) must be 9")
            return
        if 50 in msg:
            conn.reject_logon(sender, "SenderSubID(50) not allowed on Logon")
            return
        s = self.session(sender)
        if s.conn is not None:
            conn.reject_logon(sender, "Already logged on")
            return

        reset = msg.get(141) == "Y"
        if reset:
            s.reset()
        seq = msg.get_int(34)
        conn.session = s
        if seq < s.in_expected:
            self._logout(conn, f"MsgSeqNum too low, expecting {s.in_expected} but received {seq}")
            conn.session = None
            return

        s.conn = conn
        conn.heartbeat_s = msg.get_int(108, 30)
        s.received_types["A"] += 1
        s.logon_seqs.append(seq)
        body: Fields = [(98, "0"), (108, str(conn.heartbeat_s))]
        if reset:
            body.append((141, "Y"))
        s.send("A", body)
        log.info("%s logged on (seq %d)", sender, seq)
        if seq > s.in_expected:
            s.send("2", [(7, str(s.in_expected)), (16, "0")])
        else:
            s.in_expected = seq + 1

    def _logout(self, conn: SimConnection, text: str) -> None:
        log.info("logging out %s: %s", conn.session.sender if conn.session else "?", text)
        conn.logout_sent = True
        if conn.session is not None:
            conn.session.send("5", [(58, text)])
        conn.close()

    def _resend(self, conn: SimConnection, begin: int, end: int) -> None:
        s = conn.session
        assert s is not None
        last = s.out_seq - 1 if end == 0 else min(end, s.out_seq - 1)
        gap_start: int | None = None
        for seq in range(begin, last + 1):
            if seq in s.history:
                if gap_start is not None:
                    conn.write("4", [(123, "Y"), (36, str(seq))], gap_start, poss_dup=True)
                    gap_start = None
                msg_type, body = s.history[seq]
                conn.write(msg_type, body, seq, poss_dup=True)
            elif gap_start is None:
                gap_start = seq
        if gap_start is not None:
            conn.write("4", [(123, "Y"), (36, str(last + 1))], gap_start, poss_dup=True)


async def serve(port: int, passwords: dict[str, str]) -> None:
    """Run a gateway until cancelled."""
    gw = SimGateway(passwords)
    await gw.start(port=port)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
    await gw.stop()
