"""Resting client orders survive a simulator restart, as they would at the exchange."""

from trading_fix import FixMessage
from trading_sim import BID, SimGateway, SimInstrument, SimMarketData, SimOrderEntry

SENDER = "TEST-OE01"
SYMBOL = "BFUT500-ERCOT-HB_NORTH-261231-15"


def start(state_path):
    md = SimMarketData(SimGateway({}), [SimInstrument(SYMBOL, fair=45.0)], seed=1)
    gw = SimGateway({SENDER: "sim"})
    return md, SimOrderEntry(gw, md, state_path=state_path), gw


def new_order(cl, price):
    fields = [(35, "D"), (50, "trader"), (11, cl), (1, "ACC"), (55, SYMBOL), (54, "1"), (38, "2")]
    return FixMessage(fields + [(40, "2"), (44, f"{price:.2f}"), (59, "1")])


def test_resting_orders_survive_a_restart(tmp_path):
    path = tmp_path / "orders.json"
    md, exchange, gw = start(path)
    exchange._on_message(gw.session(SENDER), new_order("A", md.best(SYMBOL, BID) - 1.0))
    order_id = exchange.by_cl_ord_id[(SENDER, "A")].order_id

    md2, exchange2, gw2 = start(path)  # a new simulator process, same state file
    assert exchange2.by_cl_ord_id[(SENDER, "A")].order_id == order_id
    resting = md2.books[SYMBOL][order_id]
    assert resting.owner == SENDER and resting.qty == 2
    assert md2.new_id("O") != md.new_id("O")  # ids are never reused across restarts
