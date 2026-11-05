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
    state_dir: Path
    redis_url: str
    break_grace_s: float
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        if not get("DC_PASSWORD"):
            raise ConfigError("DC_PASSWORD is not set (see services/dropcopy/.env.example)")
        session = SessionConfig(
            name="dc",
            host=get("DC_HOST", "127.0.0.1"),
            port=int(get("DC_PORT", "13101")),  # drop copy shares the order entry port
            sender_comp_id=get("DC_SENDER", "CM-HURON-DC01"),
            target_comp_id=get("DC_TARGET", "EXI"),
            password=get("DC_PASSWORD"),
            heartbeat_s=int(get("DC_HEARTBEAT_S", "30")),
            reset_on_logon=False,  # drop copy keeps sequence numbers all week
        )
        tls = None
        if get("DC_TLS", "N").upper() in ("Y", "YES", "1", "TRUE"):
            tls = TlsConfig(
                ca_cert=Path(get("DC_CA_CERT", "certs/ca.crt")),
                client_cert=Path(get("DC_CLIENT_CERT", "certs/client.crt")),
                client_key=Path(get("DC_CLIENT_KEY", "certs/client.key")),
                server_name=get("DC_TLS_SERVER_NAME"),
            )
        return cls(
            session=session,
            tls=tls,
            state_dir=Path(get("DC_STATE_DIR", "var/dc")),
            redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            break_grace_s=float(get("DC_BREAK_GRACE_S", "5")),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
