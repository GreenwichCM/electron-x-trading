"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from trading_db import DbSettings


@dataclass(frozen=True)
class Settings:
    db: DbSettings
    redis_url: str
    batch: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        return cls(
            db=DbSettings.from_env(env),
            redis_url=get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            batch=int(get("RECORDER_BATCH", "500")),
            log_level=get("LOG_LEVEL", "INFO").upper(),
        )
