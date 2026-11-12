"""Database commands, run with the shared database settings:

uv run --env-file services/db.env python -m trading_db migrate    # create or update the schema
uv run --env-file services/db.env python -m trading_db status     # which migrations are applied
uv run --env-file services/db.env python -m trading_db seed-dev   # starting rules for the test users
uv run --env-file services/db.env python -m trading_db reconcile  # orderentry's reports against the drop copy's
uv run --env-file services/db.env python -m trading_db check-traders [--fill-in]  # screen orders with no trader
uv run --env-file services/db.power.env --env-file services/prod.env python -m trading_db add-user EMAIL \
    --roles trader,risk --accounts TA-HURON-00001   # the first users of a new database (production)
uv run --env-file services/db.power.env python -m trading_db clear-data [--confirm POWER]  # delete test data
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

from trading_core.environment import WrongEnvironment

from .attribution import fill_in_traders, traders_without_account, unknown_users
from .changes import ChangeError
from .connection import DbError, connect
from .migrate import MigrationError, migrate, status
from .reconcile import SETTLE_S, reconcile
from .rules import seed_dev_rules
from .settings import ConfigError, DbSettings
from .setup import add_user, clear_data, marked_as, row_counts


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trading_db")
    parser.add_argument(
        "command",
        choices=["migrate", "status", "seed-dev", "reconcile", "check-traders", "add-user", "clear-data"],
    )
    parser.add_argument("user", nargs="?", help="add-user: their ElectronX login (company email)")
    parser.add_argument("--roles", default="", help="add-user: trader, risk, management (comma-separated)")
    parser.add_argument("--accounts", default="", help="add-user: ElectronX accounts they may trade on")
    parser.add_argument("--name", default="", help="add-user: their name (default: the email before @)")
    parser.add_argument("--confirm", default="", help="clear-data: the database's name, to really delete")
    parser.add_argument("--hours", type=float, default=24, help="reconcile: how far back to look")
    parser.add_argument(
        "--fill-in", action="store_true", help="check-traders: give blank rows to whoever has their id now"
    )
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
        elif args.command == "check-traders":
            sys.exit(0 if check_traders(settings, args.fill_in) else 1)
        elif args.command == "add-user":
            add_first_user(settings, args)
        elif args.command == "clear-data":
            clear(settings, args.confirm)
        elif seed_dev_rules(settings):
            print(f"{settings!r}: added the test users (alice, bob, carol, dave, erin @example.test) and their rules")
        else:
            print(f"{settings!r}: electronx.users already has rows; nothing changed")
    except (ConfigError, MigrationError, WrongEnvironment, *DbError) as exc:  # DbError is a tuple of classes
        sys.exit(f"error: {exc}")


def add_first_user(settings: DbSettings, args: argparse.Namespace) -> None:
    """One of the first users of a new database (see setup.py)."""
    if not args.user or not args.roles:
        sys.exit("error: add-user needs the email and --roles (and --accounts for a trader)")
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    with closing(connect(settings)) as conn:
        try:
            added = add_user(conn, args.user, roles, accounts, args.name)
        except ChangeError as exc:
            sys.exit(f"error: {exc}")
    print(f"{settings!r}: added {args.user.strip().lower()} ({', '.join(sorted(roles))})")
    if added:
        print(f"  and the ElectronX account {', '.join(added)} (capacity 2)")


def clear(settings: DbSettings, confirm: str) -> None:
    """Show what the electronx tables hold; with --confirm <database name>, delete all of it."""
    with closing(connect(settings, check_environment=False)) as conn:  # it may be another environment's
        counts, mark = row_counts(conn), marked_as(conn)
        print(f"{settings!r}: marked as {mark or 'nobody'}'s")
        for table, n in counts.items():
            print(f"  electronx.{table}: {n} rows")
        if confirm != settings.database:
            print(f"nothing deleted: to delete every row above, add --confirm {settings.database}")
            return
        try:
            deleted = clear_data(conn)
        except ChangeError as exc:
            sys.exit(f"error: {exc}")
    print(f"deleted {sum(deleted.values())} rows; the tables are empty and the database is marked by nobody")


def check_traders(settings: DbSettings, fill_in: bool) -> bool:
    """Orders from the ElectronX screen recorded without a trader, and traders with no ElectronX account.
    True if every screen order has its trader."""
    with closing(connect(settings)) as conn:
        if fill_in:
            changed = fill_in_traders(conn)
            conn.commit()
        unknown = unknown_users(conn)
        missing = traders_without_account(conn)
    print(f"{settings!r}")
    if fill_in:
        print(f"  filled in the trader on {changed['orders']} orders and {changed['order_events']} order events")
    if missing:
        print(f"  active traders with no ElectronX account to trade on: {', '.join(missing)}")
    if not unknown:
        print("  every order from the ElectronX screen has its trader")
        return True
    print("  ElectronX users whose orders from the screen have no trader:")
    for u in unknown:
        last = f"{u.last_seen:%Y-%m-%d %H:%M}" if u.last_seen else "?"
        hint = (
            f"{u.owner} is a user now: run with --fill-in"
            if u.owner
            else "nobody here has this ElectronX login: add them as a user on the Trading rules page"
        )
        print(f"    {u.exchange_user or '(none sent)'}  {u.orders} orders, last {last} UTC  {hint}")
    return False


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
