"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from trading_db import ConfigError, DbSettings

from .day import TradingDay


@dataclass(frozen=True)
class Settings:
    db: DbSettings  # loss and position limits come from the trading rules in SQL Server
    rules_refresh_s: float
    max_spread: float
    publish_s: float
    trading_day: TradingDay  # daily P&L and the daily loss limit run over it
    redis_url: str
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        try:
            day = TradingDay.parse(get("RISK_DAY_TZ", "America/New_York"), get("RISK_DAY_START", "00:00"))
        except ValueError as exc:
            raise ConfigError(f"RISK_DAY_TZ / RISK_DAY_START: {exc}") from exc
        return cls(
            db=DbSettings.from_env(env),
            rules_refresh_s=float(get("RULES_REFRESH_S", "5")),
            max_spread=float(get("RISK_MAX_SPREAD", "5")),
            publish_s=float(get("RISK_PUBLISH_S", "1")),
            trading_day=day,
            redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
