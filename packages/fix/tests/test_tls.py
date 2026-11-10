"""Mutual TLS, as the real ElectronX gateways require: a FIX logon over TLS against the simulator, and the
connection check. The certificates are made for each run with openssl (skipped without it)."""

import asyncio
import shutil
import ssl
import subprocess
import threading
import time

import pytest
from trading_fix import FixSession, SeqStore, SessionConfig, TlsConfig, build_ssl_context
from trading_fix.check import check
from trading_sim import SimGateway

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def make_ca(folder, name):
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.crt",
            "-days", "2", "-subj", f"/CN={name}", cwd=folder)  # fmt: skip


def make_cert(folder, name, ca):
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", f"/CN={name}", cwd=folder)  # fmt: skip
    openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.crt", "-CAkey", f"{ca}.key", "-CAcreateserial",
            "-out", f"{name}.crt", "-days", "2", cwd=folder)  # fmt: skip
    (folder / f"{name}.key").chmod(0o600)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("certs")
    make_ca(folder, "ca")
    make_cert(folder, "gateway", "ca")
    make_cert(folder, "client", "ca")
    make_ca(folder, "other-ca")
    make_cert(folder, "stranger", "other-ca")  # a client certificate the gateway does not trust
    return folder


def gateway_context(certs):
    """Like the exchange: its own certificate, and a client certificate signed by our CA required."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(certs / "ca.crt"))
    ctx.load_cert_chain(str(certs / "gateway.crt"), str(certs / "gateway.key"))
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_tls(certs, name="client"):
    return TlsConfig(certs / "ca.crt", certs / f"{name}.crt", certs / f"{name}.key")


class Quiet:
    def on_logon(self, session):
        pass

    def on_disconnect(self, session):
        pass

    def on_app_message(self, session, msg):
        pass


def test_fix_logon_over_mutual_tls(certs):
    async def scenario():
        gw = SimGateway({"TEST-OE01": "sim"})
        port = await gw.start(ssl_context=gateway_context(certs))
        cfg = SessionConfig(
            name="oe", host="127.0.0.1", port=port, sender_comp_id="TEST-OE01", target_comp_id="EXI", password="sim"
        )
        session = FixSession(cfg, Quiet(), SeqStore(), build_ssl_context(client_tls(certs)))
        task = asyncio.create_task(session.run())
        deadline = time.monotonic() + 5
        while not session.logged_on:
            assert time.monotonic() < deadline, "no logon over TLS"
            await asyncio.sleep(0.02)
        await session.stop()
        await task
        await gw.stop()

    asyncio.run(asyncio.wait_for(scenario(), 20))


class TlsServer:
    """A plain TLS listener in a thread, for the connection check."""

    def __init__(self, context):
        self.sock = context.wrap_socket(__import__("socket").create_server(("127.0.0.1", 0)), server_side=True)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except ssl.SSLError:
                continue  # a client the gateway does not trust
            except OSError:
                return
            conn.close()

    def close(self):
        self.sock.close()


def env(port, certs, name="client", tls="Y"):
    return {
        "OE_HOST": "127.0.0.1",
        "OE_PORT": str(port),
        "OE_TLS": tls,
        "OE_CA_CERT": str(certs / "ca.crt"),
        "OE_CLIENT_CERT": str(certs / f"{name}.crt"),
        "OE_CLIENT_KEY": str(certs / f"{name}.key"),
    }


def test_the_connection_check(certs):
    server = TlsServer(gateway_context(certs))
    try:
        lines = []
        assert check("OE", env(server.port, certs), lines.append)
        assert any(line.startswith("ok   TLS") for line in lines), lines
        assert any("exchange certificate: subject commonName=gateway" in line for line in lines), lines

        lines = []
        assert not check("OE", {**env(server.port, certs), "OE_CLIENT_KEY": str(certs / "missing.key")}, lines.append)
        assert "certificate files not found" in lines[-1]

        lines = []
        assert not check("OE", {**env(server.port, certs), "OE_PORT": "1"}, lines.append)  # nothing listens there
        assert lines[-2].startswith("FAIL TCP")
    finally:
        server.close()


def test_a_client_certificate_the_gateway_does_not_trust_is_refused(certs):
    async def scenario():
        gw = SimGateway({"TEST-OE01": "sim"})
        port = await gw.start(ssl_context=gateway_context(certs))
        cfg = SessionConfig(
            name="oe", host="127.0.0.1", port=port, sender_comp_id="TEST-OE01", target_comp_id="EXI", password="sim"
        )
        session = FixSession(cfg, Quiet(), SeqStore(), build_ssl_context(client_tls(certs, "stranger")))
        task = asyncio.create_task(session.run())
        await asyncio.sleep(1.5)
        assert not session.logged_on
        await session.stop()
        await task
        await gw.stop()

    asyncio.run(asyncio.wait_for(scenario(), 20))
