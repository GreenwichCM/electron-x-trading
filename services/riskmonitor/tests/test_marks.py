from trading_riskmonitor import Mark, choose_mark

INST = {"symbol": "X", "low_limit": 0.0, "high_limit": 100.0}


def book(bid, ask, last=None, stale=False):
    return {"symbol": "X", "bid": bid, "ask": ask, "stale": stale, "last_trade": {"price": last} if last else None}


def test_tight_two_sided_book_marks_at_mid():
    m = choose_mark(book(40.0, 41.0), INST, None, now=10, max_spread=5)
    assert (m.price, m.source, m.stale) == (40.5, "mid", False)


def test_wide_book_falls_back_to_a_last_trade_inside_it():
    m = choose_mark(book(30.0, 50.0, last=42.0), INST, None, now=10, max_spread=5)
    assert (m.price, m.source) == (42.0, "last trade")


def test_last_trade_outside_the_book_is_not_used():
    previous = Mark(41.0, "mid", time=1)
    m = choose_mark(book(30.0, 50.0, last=55.0), INST, previous, now=10, max_spread=5)
    assert (m.price, m.source, m.stale, m.time) == (41.0, "mid", True, 1)  # previous mark, marked stale


def test_one_sided_book_uses_last_trade():
    m = choose_mark(book(40.0, None, last=44.0), INST, None, now=10, max_spread=5)
    assert (m.price, m.source) == (44.0, "last trade")


def test_stale_book_keeps_the_previous_mark():
    previous = Mark(41.0, "mid", time=1)
    m = choose_mark(book(40.0, 41.0, stale=True), INST, previous, now=10, max_spread=5)
    assert m.stale and m.price == 41.0


def test_nothing_usable_and_no_history():
    m = choose_mark(None, INST, None, now=10, max_spread=5)
    assert m.price is None and m.stale


def test_marks_are_clamped_to_the_contract_limits():
    m = choose_mark(book(99.5, 101.5), INST, None, now=10, max_spread=5)
    assert m.price == 100.0
