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
    assert rules.check_trader("alice", ERCOT) is None and "not entitled" in rules.check_trader("alice", PJM)
    assert rules.check_trader("dave", PJM) is None and "not entitled" in rules.check_trader("dave", ERCOT)
    assert rules.check_trader("bob", ERCOT) == "bob is not set up to trade"
    alice = rules.limits_for("alice", ERCOT)
    assert (alice.max_order_qty, alice.max_position, alice.max_loss) == (10, 20, 5000.0)
    assert rules.limits_for("dave", PJM).max_position == 50


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
            "UPDATE electronx.traders SET active = 0, exchange_trader_id = 'x' WHERE trader_id = N'dave'",
        )
        for _ in range(100):
            if len(seen) > 1:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)  # and nothing more while nothing changes
        task.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert len(seen) == 2
    assert seen[0].check_trader("dave", PJM) is None
    assert seen[1].check_trader("dave", PJM) == "dave is not active" and seen[1].exchange_trader("dave") == "x"
