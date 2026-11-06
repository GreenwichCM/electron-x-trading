"""Database commands, run with the shared database settings:

uv run --env-file services/db.env python -m trading_db migrate    # create or update the schema
uv run --env-file services/db.env python -m trading_db status     # which migrations are applied
uv run --env-file services/db.env python -m trading_db seed-dev   # starting rules for the test users
"""

from __future__ import annotations

import argparse
import logging
import sys

from .connection import DbError
from .migrate import MigrationError, migrate, status
from .rules import seed_dev_rules
from .settings import ConfigError, DbSettings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trading_db")
    parser.add_argument("command", choices=["migrate", "status", "seed-dev"])
    args = parser.parse_args()
    logging.basicConfig(level="INFO", format="%(message)s")
    try:
        settings = DbSettings.from_env()
        if args.command == "migrate":
            done = migrate(settings)
            print(f"{settings!r}: " + (f"applied {', '.join(done)}" if done else "already up to date"))
        elif args.command == "status":
            applied, pending = status(settings)
            print(f"{settings!r}")
            print(f"applied: {', '.join(applied) or 'none'}")
            print(f"pending: {', '.join(pending) or 'none'}")
        elif seed_dev_rules(settings):
            print(f"{settings!r}: added starting rules for alice (ERCOT) and dave (PJM)")
        else:
            print(f"{settings!r}: electronx.traders already has rows; nothing changed")
    except (ConfigError, MigrationError, *DbError) as exc:  # DbError is a tuple of classes
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
