from trading_dropcopy import Reconciler

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
