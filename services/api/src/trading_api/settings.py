"""Settings from environment variables (an env_file in Docker Compose, or `uv run --env-file`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://127.0.0.1:6379/0"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        return cls(
            redis_url=get("REDIS_URL", cls.redis_url),
            host=get("API_HOST", cls.host),
            port=int(get("API_PORT", str(cls.port))),
            log_level=get("LOG_LEVEL", cls.log_level).upper(),
        )
