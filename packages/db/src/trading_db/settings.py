"""Where the database is, from environment variables (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
DB_ENCRYPT)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DbSettings:
    host: str = "127.0.0.1"
    port: int = 1433
    database: str = "trading"
    user: str = "sa"
    password: str = ""
    encrypt: bool = True  # TLS to the server; see connect() for what it does and does not check

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> DbSettings:
        def get(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        if not get("DB_PASSWORD"):
            raise ConfigError("DB_PASSWORD is not set (see services/recorder/.env.example)")
        return cls(
            host=get("DB_HOST", "127.0.0.1"),
            port=int(get("DB_PORT", "1433")),
            database=get("DB_NAME", "trading"),
            user=get("DB_USER", "sa"),
            password=get("DB_PASSWORD"),
            encrypt=get("DB_ENCRYPT", "Y").upper() in ("Y", "YES", "1", "TRUE"),
        )

    def on(self, database: str) -> DbSettings:
        """The same server, another database."""
        return replace(self, database=database)

    def __repr__(self) -> str:  # never print the password
        return f"DbSettings({self.user}@{self.host}:{self.port}/{self.database}, encrypt={self.encrypt})"
