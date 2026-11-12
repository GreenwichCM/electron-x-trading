"""Trading rules in SQL Server: seeding, loading, and following changes (skipped without SQL Server)."""

import asyncio
from contextlib import closing

from trading_db import connect, follow_rules, load_rules, migrate, seed_dev_rules

ERCOT = "BFUT500-ERCOT-HB_NORTH-261231-15"
PJM = "BFUT100-PJM-WESTERN_HUB-261231-15"


def execute(settings, sql):
    with closing(connect(settings, autocommit=True)) as conn:
        conn.cursor().execute(sql)


def rules_now(settings):
    with closing(connect(settings)) as conn:
        return load_rules(conn)


def test_dev_seed_loads_into_rules(sql_server):
    migrate(sql_server)
    assert seed_dev_rules(sql_server)
    assert not seed_dev_rules(sql_server)  # never twice, never over rules someone set
    rules = rules_now(sql_server)
    assert rules.check_trader("alice@example.test", ERCOT) is None and "not entitled" in rules.check_trader(
        "alice@example.test", PJM
    )
    assert rules.check_trader("dave@example.test", PJM) is None and "not entitled" in rules.check_trader(
        "dave@example.test", ERCOT
    )
    assert rules.check_trader("bob@example.test", ERCOT) == "bob@example.test is not a trader (roles: risk)"
    assert rules.account_for("alice@example.test")[0].account_id == "TA-HURON-00001"
    assert rules.traders["carol@example.test"].name == "Carol (management)"
    alice = rules.limits_for("alice@example.test", ERCOT)
    assert (alice.max_order_qty, alice.max_position, alice.max_loss) == (10, 20, 5000.0)
    assert rules.limits_for("dave@example.test", PJM).max_position == 50


def test_follow_rules_sees_changes(sql_server):
    migrate(sql_server)
    seed_dev_rules(sql_server)
    seen = []

    async def scenario():
        task = asyncio.create_task(follow_rules(sql_server, seen.append, every_s=0.1))
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.05)
        await asyncio.to_thread(
            execute,
            sql_server,
            "UPDATE electronx.users SET active = 0 WHERE user_id = N'dave@example.test'",
        )
        for _ in range(100):
            if len(seen) > 1:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)  # and nothing more while nothing changes
        task.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert len(seen) == 2
    assert seen[0].check_trader("dave@example.test", PJM) is None
    assert seen[1].check_trader("dave@example.test", PJM) == "dave@example.test is not active"
