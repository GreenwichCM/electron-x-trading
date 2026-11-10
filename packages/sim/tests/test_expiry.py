"""The simulator's contract expiry and settlement."""

import time

from trading_sim import BID, OFFER, SimGateway, SimInstrument, SimMarketData

LIVE = "BFUT500-ERCOT-HB_NORTH-261231-15"
ENDING = "BFUT100-ERCOT-HB_HOUSTON-261110-15"


def test_expiry_cancels_every_order_then_the_settlement_follows():
    ending = SimInstrument(ENDING, fair=120.0, high=100.0, last_trade_at=time.time() + 60, settle_after_s=30)
    md = SimMarketData(SimGateway({}), [SimInstrument(LIVE), ending], seed=1)
    expired = []
    md.on_expire = lambda symbol, order: expired.append((symbol, order.order_id))
    client = md.add_order(ENDING, BID, 300, 2, owner="TEST-OE01")

    md.step()
    assert ending.state == "OPEN" and md.books[ENDING]

    ending.last_trade_at = time.time() - 1  # trading has ended
    md.step()
    assert ending.state == "EXPIRED" and md.books[ENDING] == {} and expired == [(ENDING, client)]
    assert ending.settlement is None
    before = dict(md.books[ENDING])
    md.step()
    assert md.books[ENDING] == before  # no more activity

    ending.last_trade_at = time.time() - 31
    md.step()
    assert ending.settlement == 100.0  # fair 120, bounded by the ceiling
    assert md.best(LIVE, BID) is not None and md.best(LIVE, OFFER) is not None  # the others carry on
