from trading_marketdata import BID, OFFER, OrderBook


def test_new_change_delete():
    b = OrderBook("X")
    b.apply("0", "1", BID, 40.0, 5)
    b.apply("0", "2", BID, 40.25, 3)
    b.apply("0", "3", OFFER, 41.0, 2)
    assert (b.best_bid(), b.best_ask(), b.mid()) == (40.25, 41.0, 40.625)
    b.apply("1", "2", "", None, 1)  # change keeps side and price when not sent
    assert b.orders["2"] == (BID, 40.25, 1)
    b.apply("2", "2", "", None, 0)
    assert b.best_bid() == 40.0


def test_depth_aggregates_orders_at_one_price():
    b = OrderBook("X")
    b.apply("0", "1", BID, 40.0, 5)
    b.apply("0", "2", BID, 40.0, 2)
    b.apply("0", "3", BID, 39.75, 1)
    assert b.depth(BID) == [(40.0, 7), (39.75, 1)]
    assert b.depth(BID, levels=1) == [(40.0, 7)]


def test_trades_are_not_resting_orders():
    b = OrderBook("X")
    b.apply("0", "T1", "2", 40.0, 1)
    assert b.orders == {}


def test_excluding_own_orders_from_the_mid():
    b = OrderBook("X")
    b.apply("0", "mine", BID, 40.5, 1)
    b.apply("0", "theirs", BID, 40.0, 1)
    b.apply("0", "ask", OFFER, 41.0, 1)
    assert b.best_bid(exclude={"mine"}) == 40.0
