"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trading_fix import SessionConfig, TlsConfig


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    session: SessionConfig
    tls: TlsConfig | None
    symbols: tuple[str, ...]
    list_refresh_s: float  # how often the instrument list is requested again, for new contracts
    keep_unsettled_s: float  # how long a contract that left the list is followed, waiting for settlement
    redis_url: str
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        password = get("MD_PASSWORD")
        if not password:
            raise ConfigError("MD_PASSWORD is not set (see services/marketdata/.env.example)")
        session = SessionConfig(
            name="md",
            host=get("MD_HOST", "127.0.0.1"),
            port=int(get("MD_PORT", "13001")),
            sender_comp_id=get("MD_SENDER", "HURON-MD01"),
            target_comp_id=get("MD_TARGET", "EXI"),
            password=password,
            heartbeat_s=int(get("MD_HEARTBEAT_S", "30")),
            reset_on_logon=True,  # the MD gateway resets sequence numbers on every logon
        )
        tls = None
        if get("MD_TLS", "N").upper() in ("Y", "YES", "1", "TRUE"):
            tls = TlsConfig(
                ca_cert=Path(get("MD_CA_CERT", "certs/ca.crt")),
                client_cert=Path(get("MD_CLIENT_CERT", "certs/client.crt")),
                client_key=Path(get("MD_CLIENT_KEY", "certs/client.key")),
                server_name=get("MD_TLS_SERVER_NAME"),
            )
        symbols = tuple(s.strip() for s in get("MD_SYMBOLS").split(",") if s.strip())
        return cls(
            session=session,
            tls=tls,
            symbols=symbols,
            list_refresh_s=float(get("MD_LIST_REFRESH_S", "300")),
            keep_unsettled_s=float(get("MD_KEEP_UNSETTLED_S", "259200")),
            redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
