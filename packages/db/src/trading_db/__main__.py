"""Database commands, run with the shared database settings:

uv run --env-file services/db.env python -m trading_db migrate    # create or update the schema
uv run --env-file services/db.env python -m trading_db status     # which migrations are applied
uv run --env-file services/db.env python -m trading_db seed-dev   # starting rules for the test users
uv run --env-file services/db.env python -m trading_db reconcile  # orderentry's reports against the drop copy's
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

from .connection import DbError, connect
from .migrate import MigrationError, migrate, status
from .reconcile import SETTLE_S, reconcile
from .rules import seed_dev_rules
from .settings import ConfigError, DbSettings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trading_db")
    parser.add_argument("command", choices=["migrate", "status", "seed-dev", "reconcile"])
    parser.add_argument("--hours", type=float, default=24, help="reconcile: how far back to look")
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
        elif args.command == "reconcile":
            sys.exit(0 if print_reconciliation(settings, args.hours) else 1)
        elif seed_dev_rules(settings):
            print(f"{settings!r}: added starting rules for alice (ERCOT) and dave (PJM)")
        else:
            print(f"{settings!r}: electronx.traders already has rows; nothing changed")
    except (ConfigError, MigrationError, *DbError) as exc:  # DbError is a tuple of classes
        sys.exit(f"error: {exc}")


def print_reconciliation(settings: DbSettings, hours: float) -> bool:
    until = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=SETTLE_S)
    with closing(connect(settings)) as conn:
        r = reconcile(conn, since=until - timedelta(hours=hours), until=until)
    print(f"{settings!r}: our orders' ExecutionReports from {r.since:%Y-%m-%d %H:%M} to {r.until:%H:%M} UTC")
    print(f"  drop copy: {r.drop_copy_reports}   orderentry's session: {r.orderentry_reports}")
    for title, rows in (
        ("on the drop copy, not recorded by orderentry", r.drop_copy_only),
        ("recorded by orderentry, not on the drop copy", r.orderentry_only),
    ):
        print(f"  {title}: {len(rows) if len(rows) < 50 else '50 or more'}")
        for row in rows:
            print("    " + "  ".join("" if v is None else str(v) for v in row))
    print("  all match" if r.ok else "  MISMATCH: see above")
    return r.ok


if __name__ == "__main__":
    main()
