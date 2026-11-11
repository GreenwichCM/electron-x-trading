from trading_dropcopy import ExchangeOrder, Reconciler

X = "BFUT500-ERCOT-HB_NORTH-261231-15"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def order(cl, side, cum, symbol=X):
    return {"cl_ord_id": cl, "symbol": symbol, "side": side, "cum_qty": cum}


def test_matching_positions_are_fine():
    r = Reconciler(grace_s=5, clock=Clock())
    r.on_order(order("A", "BUY", 3))
    r.on_order(order("B", "SELL", 1))
    assert r.orderentry_positions() == {X: 2}
    assert r.check({X: 2}) is None


def test_a_short_difference_is_not_a_break():
    clock = Clock()
    r = Reconciler(grace_s=5, clock=clock)
    assert r.check({X: 1}) is None  # drop copy arrived first
    clock.now = 2
    r.on_order(order("A", "BUY", 1))  # orderentry catches up
    clock.now = 10
    assert r.check({X: 1}) is None


def test_a_lasting_difference_is_a_break():
    clock = Clock()
    r = Reconciler(grace_s=5, clock=clock)
    r.on_order(order("A", "BUY", 1))
    assert r.check({X: 3}) is None
    clock.now = 5
    assert r.check({X: 3}) == f"position break on {X}: exchange +3, orderentry +1"


def test_filled_quantity_never_goes_backwards():
    r = Reconciler(clock=Clock())
    r.on_order(order("A", "BUY", 2))
    r.on_order(order("A", "BUY", 0))  # an older update read late
    assert r.orderentry_positions() == {X: 2}


def exchange_order(order_id, cl, filled, user="firms/h/users/alice", first_seen=0.0):
    found = ExchangeOrder(order_id, cl, user, X, filled=filled, first_seen=first_seen)
    found.cl_ord_ids.add(cl)
    return found


def test_orders_orderentry_never_sent_came_from_the_screen_and_are_never_a_break():
    clock = Clock()
    clock.now = 10
    r = Reconciler(grace_s=5, screen_after_s=2, clock=clock)
    r.on_order({**order("A", "BUY", 2), "order_id": "O1"})
    r.on_order({**order("B", "SELL", 1), "order_id": ""})
    ours = exchange_order("O1", "CANCEL-7", 2)  # known by its OrderID: a fill after a cancel request
    also_ours = exchange_order("O2", "B", -1)  # known by its ClOrdID, not acknowledged in the stream yet
    screen = exchange_order("O3", "UI-1", -3, user="firms/h/users/dave")
    just_seen = exchange_order("O4", "UI-2", 1, first_seen=9.5)  # unknown for only half a second: pending
    split = r.split([ours, also_ours, screen, just_seen])
    assert split.ours == {X: 1} and split.screen == {X: -3} and split.pending == {X: 1}
    assert split.screen_by_user == {"firms/h/users/dave": {X: -3}} and split.screen_orders == [screen]
    assert r.check(split.ours) is None
    clock.now = 30
    assert r.check(r.split([ours, also_ours, screen, just_seen]).ours) is None


def test_a_bust_takes_back_what_orderentry_filled():
    r = Reconciler(clock=Clock())
    r.on_order({**order("A", "BUY", 2), "version": 3})
    r.on_order({**order("A", "BUY", 2), "version": 4, "busted_qty": 1})
    r.on_order({**order("A", "BUY", 2), "version": 3})  # an older update read late changes nothing
    assert r.orderentry_positions() == {X: 1}
