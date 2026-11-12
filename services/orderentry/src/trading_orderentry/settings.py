"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trading_db import ConfigError as DbConfigError
from trading_db import DbSettings
from trading_fix import SessionConfig, TlsConfig


class ConfigError(Exception):
    pass


#: Settings from before step 25: each trader's own login and the accounts in SQL Server replace them.
NO_LONGER_USED = ("OE_DEFAULT_TRADER", "OE_ACCOUNT", "OE_CAPACITY")


@dataclass(frozen=True)
class Settings:
    session: SessionConfig
    tls: TlsConfig | None
    state_dir: Path
    db: DbSettings  # users, accounts and the trading rules come from SQL Server
    rules_refresh_s: float
    rate_limit: int
    redis_url: str
    control_host: str
    control_port: int
    internal_token: str
    log_level: str = field(default="INFO")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        if env.get("TRADING_ENV", "").strip().lower() == "production":
            raise ConfigError(
                "production is record-only for now: orders are entered on the ElectronX screen, and orderentry "
                "does not run until sign-in is in place (see Production in README.md)"
            )
        missing = [k for k in ("OE_PASSWORD", "OE_INTERNAL_TOKEN") if not get(k)]
        if missing:
            raise ConfigError(f"not set: {', '.join(missing)} (see services/orderentry/.env.example)")
        if len(get("OE_INTERNAL_TOKEN")) < 32:
            raise ConfigError("OE_INTERNAL_TOKEN must be at least 32 characters")

        session = SessionConfig(
            name="oe",
            host=get("OE_HOST", "127.0.0.1"),
            port=int(get("OE_PORT", "13101")),
            sender_comp_id=get("OE_SENDER", "D-HURON-01-OE01"),
            target_comp_id=get("OE_TARGET", "EXI"),
            password=get("OE_PASSWORD"),
            heartbeat_s=int(get("OE_HEARTBEAT_S", "30")),
            reset_on_logon=False,  # order entry keeps sequence numbers all week
        )
        tls = None
        if get("OE_TLS", "N").upper() in ("Y", "YES", "1", "TRUE"):
            tls = TlsConfig(
                ca_cert=Path(get("OE_CA_CERT", "certs/ca.crt")),
                client_cert=Path(get("OE_CLIENT_CERT", "certs/client.crt")),
                client_key=Path(get("OE_CLIENT_KEY", "certs/client.key")),
                server_name=get("OE_TLS_SERVER_NAME"),
            )
        try:
            db = DbSettings.from_env(env)  # services/db.env
        except DbConfigError as exc:
            raise ConfigError(str(exc)) from exc
        return cls(
            session=session,
            tls=tls,
            state_dir=Path(get("OE_STATE_DIR", "var/oe")),
            db=db,
            rules_refresh_s=float(get("RULES_REFRESH_S", "5")),
            rate_limit=int(get("OE_RATE_LIMIT", "90")),
            redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            control_host=get("OE_CONTROL_HOST", "127.0.0.1"),
            control_port=int(get("OE_CONTROL_PORT", "8100")),
            internal_token=get("OE_INTERNAL_TOKEN"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
