"""Check the way to an ElectronX gateway before logging on: the network path, then mutual TLS.

    uv run --env-file services/marketdata/.env --env-file services/marketdata/uat.env python -m trading_fix.check MD

The argument is the settings prefix: MD (market data), OE (order entry) or DC (drop copy). It reads
<PREFIX>_HOST, _PORT, _TLS, _CA_CERT, _CLIENT_CERT, _CLIENT_KEY and _TLS_SERVER_NAME, opens a TCP
connection and, with TLS, completes the handshake with the client certificate. It sends no FIX message,
so it uses no FIX password and leaves no trace in the session's sequence numbers.
"""

from __future__ import annotations

import os
import socket
import ssl
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from .tls import TlsConfig, build_ssl_context

TIMEOUT_S = 10.0


def _cert_summary(cert: dict) -> str:
    def name(part: tuple) -> str:
        return ", ".join(f"{k}={v}" for rdn in part for k, v in rdn)

    subject, issuer = name(cert.get("subject", ())), name(cert.get("issuer", ()))
    return f"subject {subject}; issuer {issuer}; expires {cert.get('notAfter')}"


def check(prefix: str, env: Mapping[str, str] = os.environ, out=print) -> bool:
    def get(key: str, default: str = "") -> str:
        return env.get(f"{prefix}_{key}", "").strip() or default

    host, port = get("HOST"), int(get("PORT", "0") or 0)
    if not host or not port:
        out(f"FAIL {prefix}_HOST and {prefix}_PORT must be set")
        return False
    tls = None
    if get("TLS", "N").upper() in ("Y", "YES", "1", "TRUE"):
        tls = TlsConfig(
            ca_cert=Path(get("CA_CERT", "certs/ca.crt")),
            client_cert=Path(get("CLIENT_CERT", "certs/client.crt")),
            client_key=Path(get("CLIENT_KEY", "certs/client.key")),
            server_name=get("TLS_SERVER_NAME"),
        )
        missing = [str(p) for p in (tls.ca_cert, tls.client_cert, tls.client_key) if not p.is_file()]
        if missing:
            out(f"FAIL certificate files not found: {', '.join(missing)}")
            return False
        if tls.client_key.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            out(f"WARN {tls.client_key} can be read by other users: chmod 600 {tls.client_key}")

    out(f"{prefix}: {host}:{port}, TLS {'on' if tls else 'off'}")
    started = time.monotonic()
    try:
        raw = socket.create_connection((host, port), timeout=TIMEOUT_S)
    except OSError as exc:
        out(f"FAIL TCP connection: {exc}")
        out("     Is this machine's public IP address allowed by ElectronX, and is the port open on your network?")
        return False
    out(f"ok   TCP connected in {1000 * (time.monotonic() - started):.0f} ms")
    with raw:
        if tls is None:
            return True
        try:
            context = build_ssl_context(tls)
        except (ssl.SSLError, OSError) as exc:
            out(f"FAIL loading the certificates: {exc}")
            return False
        try:
            with context.wrap_socket(raw, server_hostname=tls.server_name or None) as secure:
                out(f"ok   TLS {secure.version()}, {secure.cipher()[0]}")
                out(f"     exchange certificate: {_cert_summary(secure.getpeercert() or {})}")
        except (ssl.SSLError, OSError) as exc:
            out(f"FAIL TLS handshake: {exc}")
            out("     Check that ca.crt, client.crt and client.key are the files ElectronX sent for this environment.")
            return False
    return True


if __name__ == "__main__":
    prefixes = [a.upper() for a in sys.argv[1:]] or ["MD"]
    results = [check(p) for p in prefixes]
    sys.exit(0 if all(results) else 1)
