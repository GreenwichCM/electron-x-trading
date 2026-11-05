"""Mutual TLS for the ElectronX gateways."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TlsConfig:
    ca_cert: Path
    client_cert: Path
    client_key: Path
    server_name: str = ""  # leave blank to verify the chain without hostname matching (we connect by IP)


def build_ssl_context(tls: TlsConfig) -> ssl.SSLContext:
    """Verify the exchange against ca.crt and present client.crt / client.key."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(tls.ca_cert))
    ctx.load_cert_chain(str(tls.client_cert), str(tls.client_key))
    # Python 3.13+ turns on strict X.509 checks by default; ElectronX's certificates lack the Authority
    # Key Identifier those require. The chain is still verified against ca.crt.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    ctx.check_hostname = bool(tls.server_name)
    return ctx
