"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trading_fix import SessionConfig, TlsConfig

from .risk import RiskLimits


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    session: SessionConfig
    tls: TlsConfig | None
    state_dir: Path
    account: str
    capacity: str
    default_trader: str
    trader_ids: dict[str, str]
    limits: RiskLimits
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

        missing = [k for k in ("OE_PASSWORD", "OE_DEFAULT_TRADER", "OE_INTERNAL_TOKEN") if not get(k)]
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
        trader_ids = dict(
            pair.split("=", 1) for pair in get("OE_TRADER_IDS").split(",") if "=" in pair
        )  # e.g. alice=alice@example.com,bob=bob@example.com
        limits = RiskLimits(
            max_order_qty=int(get("OE_MAX_ORDER_QTY", "10")),
            max_order_notional=float(get("OE_MAX_ORDER_NOTIONAL", "5000")),
            max_open_orders=int(get("OE_MAX_OPEN_ORDERS", "20")),
            max_position=int(get("OE_MAX_POSITION", "50")),
        )
        return cls(
            session=session,
            tls=tls,
            state_dir=Path(get("OE_STATE_DIR", "var/oe")),
            account=get("OE_ACCOUNT", "TA-HURON-00001"),
            capacity=get("OE_CAPACITY", "2"),
            default_trader=get("OE_DEFAULT_TRADER"),
            trader_ids={k.strip(): v.strip() for k, v in trader_ids.items()},
            limits=limits,
            rate_limit=int(get("OE_RATE_LIMIT", "90")),
            redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            control_host=get("OE_CONTROL_HOST", "127.0.0.1"),
            control_port=int(get("OE_CONTROL_PORT", "8100")),
            internal_token=get("OE_INTERNAL_TOKEN"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
