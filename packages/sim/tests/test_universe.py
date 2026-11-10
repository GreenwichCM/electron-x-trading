"""The simulator's full universe: every hour of today and tomorrow, rolling on as days go by."""

import random
from datetime import UTC, datetime

from trading_core import Symbol
from trading_sim import SimGateway, SimMarketData
from trading_sim.universe import full_universe, roll

NOW = datetime(2026, 11, 10, 15, 30, tzinfo=UTC).timestamp()  # 09:30 in Houston, 10:30 in New York


def test_every_hour_still_trading_today_and_all_of_tomorrow():
    insts = {i.symbol: i for i in full_universe(NOW, random.Random(1))}
    north = sorted(s for s in insts if s.startswith("BFUT500-ERCOT-HB_NORTH-"))
    # HE10 (09:00-10:00 Central) stopped trading at 09:00; HE11 trades until 10:00
    assert north[0] == "BFUT500-ERCOT-HB_NORTH-261110-11" and north[-1] == "BFUT500-ERCOT-HB_NORTH-261111-24"
    assert len(north) == 14 + 24
    pjm = sorted(s for s in insts if s.startswith("BOPT-PJM-WESTERN_HUB-"))
    assert pjm[0] == "BOPT-PJM-WESTERN_HUB-261110-12" and len(pjm) == 13 + 24  # Eastern time
    for symbol, inst in insts.items():
        assert inst.expires_at == Symbol.parse(symbol).expires_at.timestamp()
        assert inst.last_trade_at == inst.expires_at - 3600 > NOW  # trading stops when delivery begins
        assert 0 < inst.fair < inst.high
    assert insts["BOPT-ERCOT-HB_WEST-261111-01"].high == 100.0
    assert insts["BFUT5000-ERCOT-HB_WEST-261111-01"].high == 5000.0


def test_roll_lists_new_days_and_delists_settled_contracts():
    rng = random.Random(2)
    md = SimMarketData(SimGateway({}), full_universe(NOW, rng), seed=2)
    before = len(md.instruments)
    listed, delisted = roll(md, rng, now=NOW + 86400)  # a day later: the day after tomorrow comes into range
    assert listed > 0 and delisted == 0 and len(md.instruments) == before + listed
    assert "BFUT500-ERCOT-HB_NORTH-261112-24" in md.instruments

    symbol = "BFUT500-ERCOT-HB_NORTH-261110-11"
    inst = md.instruments[symbol]
    inst.settlement = 40.0
    assert roll(md, rng, now=inst.last_trade_at + inst.settle_after_s + 3600)[1] >= 1
    assert symbol not in md.instruments and symbol not in md.books


def test_with_thousands_of_contracts_only_some_move_each_step():
    md = SimMarketData(SimGateway({}), full_universe(NOW, random.Random(3)), seed=3)
    md.active_per_step = 10
    before = {s: dict(book) for s, book in md.books.items()}
    md.step()
    changed = [s for s, book in md.books.items() if book != before[s]]
    assert 0 < len(changed) <= 10
