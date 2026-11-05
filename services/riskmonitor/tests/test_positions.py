import pytest
from trading_riskmonitor import Position


def test_adding_averages_the_price():
    p = Position()
    p.apply("BUY", 2, 40.0)
    p.apply("BUY", 2, 42.0)
    assert (p.qty, p.avg_px, p.realized) == (4, 41.0, 0.0)
    assert p.unrealized(43.0) == pytest.approx(8.0)  # 4 lots x $2


def test_reducing_realizes_against_the_average():
    p = Position()
    p.apply("BUY", 4, 41.0)
    p.apply("SELL", 1, 45.0)
    assert (p.qty, p.avg_px) == (3, 41.0)
    assert p.realized == pytest.approx(4.0)


def test_shorts_make_money_when_the_price_falls():
    p = Position()
    p.apply("SELL", 2, 50.0)
    assert p.unrealized(48.0) == pytest.approx(4.0)
    p.apply("BUY", 2, 48.0)
    assert (p.qty, p.avg_px, p.realized) == (0, 0.0, pytest.approx(4.0))


def test_going_through_zero_opens_a_new_position_at_the_fill_price():
    p = Position()
    p.apply("BUY", 2, 40.0)
    p.apply("SELL", 5, 44.0)
    assert (p.qty, p.avg_px) == (-3, 44.0)
    assert p.realized == pytest.approx(8.0)


def test_no_mark_means_no_unrealized():
    p = Position()
    p.apply("BUY", 1, 40.0)
    assert p.unrealized(None) == 0.0
