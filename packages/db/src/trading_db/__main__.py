"""Schema commands: `uv run --env-file services/recorder/.env python -m trading_db migrate` (or `status`)."""

from __future__ import annotations

import argparse
import logging
import sys

from .connection import DbError
from .migrate import MigrationError, migrate, status
from .settings import ConfigError, DbSettings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trading_db")
    parser.add_argument("command", choices=["migrate", "status"])
    args = parser.parse_args()
    logging.basicConfig(level="INFO", format="%(message)s")
    try:
        settings = DbSettings.from_env()
        if args.command == "migrate":
            done = migrate(settings)
            print(f"{settings!r}: " + (f"applied {', '.join(done)}" if done else "already up to date"))
        else:
            applied, pending = status(settings)
            print(f"{settings!r}")
            print(f"applied: {', '.join(applied) or 'none'}")
            print(f"pending: {', '.join(pending) or 'none'}")
    except (ConfigError, MigrationError, DbError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
