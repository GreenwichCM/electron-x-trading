"""Loading the trading rules (electronx.traders, entitlements, limits) and keeping them current.

Services call follow_rules() once; it reloads every few seconds, so a change made in SQL Server takes
effect without a restart. If SQL Server is unreachable, the last rules loaded stay in force; until
the first load, there are none, and orderentry rejects every order (the rules fail closed).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import closing
from importlib import resources

from trading_core.rules import Limits, Trader, TradingRules

from .connection import Connection, DbError, connect
from .migrate import SCHEMA, batches
from .settings import DbSettings

log = logging.getLogger("db.rules")


def load_rules(conn: Connection) -> TradingRules:
    cur = conn.cursor()
    cur.execute(f"SELECT trader_id, exchange_trader_id, active FROM {SCHEMA}.traders")
    traders = {t: Trader(t, x or None, bool(active)) for t, x, active in cur.fetchall()}
    cur.execute(f"SELECT trader_id, market, product FROM {SCHEMA}.entitlements")
    entitlements = frozenset((t, m, p) for t, m, p in cur.fetchall())
    cur.execute(
        f"SELECT trader_id, market, product, max_order_qty, max_order_notional, max_open_orders, max_position, "
        f"max_loss FROM {SCHEMA}.limits"
    )
    limits = {}
    for t, m, p, qty, notional, open_orders, position, loss in cur.fetchall():
        limits[(t, m, p)] = Limits(
            max_order_qty=qty,
            max_order_notional=float(notional) if notional is not None else None,
            max_open_orders=open_orders,
            max_position=position,
            max_loss=float(loss) if loss is not None else None,
        )
    return TradingRules(traders=traders, entitlements=entitlements, limits=limits)


def _load(settings: DbSettings, appname: str) -> TradingRules:
    with closing(connect(settings, appname=appname)) as conn:
        return load_rules(conn)


async def follow_rules(
    settings: DbSettings,
    on_change: Callable[[TradingRules], None],
    every_s: float = 5.0,
    appname: str = "trading",
) -> None:
    """Call `on_change` with the rules now, and again whenever they change. Never returns."""
    last: TradingRules | None = None
    failing = False
    while True:
        try:
            rules = await asyncio.to_thread(_load, settings, appname)
            if rules != last:
                log.info("trading rules %s: %s", "loaded" if last is None else "changed", rules.summary())
                on_change(rules)
                last = rules
            if failing:
                log.info("trading rules are loading again")
            failing = False
        except DbError as exc:
            if not failing:  # once per outage, not every few seconds
                keep = "keeping the last rules loaded" if last else "orders are rejected until they load"
                log.warning(
                    "cannot load trading rules from %r (%s); %s (has `python -m trading_db migrate` been run?)",
                    settings,
                    exc,
                    keep,
                )
            failing = True
        await asyncio.sleep(every_s)


def seed_dev_rules(settings: DbSettings) -> bool:
    """Starting rules for the test users, only if electronx.traders is empty. True if they were added."""
    sql = (resources.files("trading_db") / "seeds" / "dev_rules.sql").read_text(encoding="utf-8")
    with closing(connect(settings)) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.traders")
        if cur.fetchone()[0]:
            return False
        for batch in batches(sql):
            cur.execute(batch)
        conn.commit()
    return True
