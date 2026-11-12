"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from trading_db import DbSettings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://127.0.0.1:6379/0"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    dev_login: bool = False
    dev_secret: str = ""
    oe_url: str = "http://127.0.0.1:8100"
    oe_token: str = ""
    db: DbSettings | None = None  # services/db.env; without it the trading rules pages answer 503
    rules_refresh_s: float = 10.0  # how often the users and trading rules are reloaded

    def __post_init__(self) -> None:
        if self.dev_login and self.host not in LOOPBACK:
            raise ConfigError(f"API_DEV_LOGIN is only allowed when API_HOST is localhost (it is {self.host})")
        if self.dev_login and len(self.dev_secret) < 32:
            raise ConfigError("API_DEV_SECRET must be at least 32 characters when API_DEV_LOGIN=Y")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        return cls(
            redis_url=get("REDIS_URL", cls.redis_url),
            host=get("API_HOST", cls.host),
            port=int(get("API_PORT", str(cls.port))),
            log_level=get("LOG_LEVEL", cls.log_level).upper(),
            dev_login=get("API_DEV_LOGIN", "N").upper() in ("Y", "YES", "1", "TRUE"),
            dev_secret=get("API_DEV_SECRET", ""),
            oe_url=get("OE_URL", cls.oe_url),
            oe_token=get("OE_INTERNAL_TOKEN", ""),
            db=DbSettings.from_env(env) if env.get("DB_PASSWORD", "").strip() else None,
            rules_refresh_s=float(get("RULES_REFRESH_S", str(cls.rules_refresh_s))),
        )
