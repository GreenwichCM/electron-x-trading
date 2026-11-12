"""SQL Server for the trading platform: settings, connections, schema migrations and trading rules."""

from trading_core.environment import WrongEnvironment

from .connection import Connection, DbError, connect
from .migrate import LATEST, SCHEMA, MigrationError, batches, load_migrations, migrate, schema_version, status
from .reconcile import Reconciliation, reconcile
from .rules import follow_rules, load_rules, seed_dev_rules
from .settings import ConfigError, DbSettings
from .setup import add_user

__all__ = [
    "LATEST",
    "SCHEMA",
    "ConfigError",
    "Connection",
    "DbError",
    "DbSettings",
    "MigrationError",
    "WrongEnvironment",
    "add_user",
    "batches",
    "connect",
    "follow_rules",
    "load_migrations",
    "load_rules",
    "migrate",
    "schema_version",
    "seed_dev_rules",
    "Reconciliation",
    "reconcile",
    "status",
]
