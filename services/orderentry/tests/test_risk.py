from trading_orderentry import Instrument, Order, RiskLimits, Status, Throttle, check_order

INST = Instrument("X", tick_size=0.25, low_limit=0.0, high_limit=100.0, state="OPEN")
LIMITS = RiskLimits(max_order_qty=10, max_order_notional=500.0, max_open_orders=3, max_position=12)


def order(side="BUY", qty=5, price=50.0, symbol="X"):
    return Order("C1", "alice", symbol, side, qty, price, "DAY")


def resting(side, qty):
    o = order(side, qty)
    o.status = Status.NEW
    return o


def test_good_order_passes():
    assert check_order(order(), INST, LIMITS, [], 0) is None


def test_each_rule():
    assert "unknown" in check_order(order(), None, LIMITS, [], 0)
    assert "HALTED" in check_order(order(), Instrument("X", 0.25, state="HALTED"), LIMITS, [], 0)
    assert "above the limit of 10" in check_order(order(qty=11, price=1.0), INST, LIMITS, [], 0)
    assert "tick" in check_order(order(price=50.1), INST, LIMITS, [], 0)
    assert "above the exchange limit" in check_order(order(qty=1, price=100.25), INST, LIMITS, [], 0)
    assert "order value" in check_order(order(qty=10, price=60.0), INST, LIMITS, [], 0)
    assert "open orders" in check_order(order(), INST, LIMITS, [resting("BUY", 1)] * 3, 0)


def test_position_limit_counts_resting_orders():
    # long 4, resting buy 5, new buy 5 -> could reach 14 > 12
    assert "could reach 14" in check_order(order("BUY", 5), INST, LIMITS, [resting("BUY", 5)], 4)
    # selling reduces the position, so it is allowed
    assert check_order(order("SELL", 5), INST, LIMITS, [resting("BUY", 5)], 4) is None
    assert "could reach -13" in check_order(order("SELL", 5), INST, LIMITS, [resting("SELL", 4)], -4)


def test_throttle_keeps_room_for_cancels():
    t = Throttle(limit=4, window_s=60, cancel_reserve=0.25)  # 3 new orders, then only cancels
    assert [t.allow() for _ in range(4)] == [True, True, True, False]
    assert t.allow(is_cancel=True) is True
    assert t.allow(is_cancel=True) is False
