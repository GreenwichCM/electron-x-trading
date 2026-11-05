from datetime import UTC, datetime

from trading_fix import SeqStore, fix_week_start


def test_week_starts_sunday_0615_utc():
    assert fix_week_start(datetime(2026, 11, 5, 12, tzinfo=UTC)) == datetime(2026, 11, 1, 6, 15, tzinfo=UTC)
    # Sunday before 06:15 still belongs to the previous week
    assert fix_week_start(datetime(2026, 11, 8, 6, 0, tzinfo=UTC)) == datetime(2026, 11, 1, 6, 15, tzinfo=UTC)
    assert fix_week_start(datetime(2026, 11, 8, 6, 15, tzinfo=UTC)) == datetime(2026, 11, 8, 6, 15, tzinfo=UTC)


def test_numbers_survive_a_restart(tmp_path):
    path = tmp_path / "oe.json"
    s = SeqStore(path)
    assert s.consume_sender() == 1
    s.consume_sender()
    s.incr_target()
    s.close()
    again = SeqStore(path)
    assert (again.next_sender, again.next_target) == (3, 2)


def test_in_memory_store_writes_nothing(tmp_path):
    s = SeqStore()
    s.consume_sender()
    assert list(tmp_path.iterdir()) == []
