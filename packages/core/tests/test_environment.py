"""Keeping development, beta and production apart: each Redis database and state folder is marked."""

import asyncio

import pytest
from fakeredis import FakeAsyncRedis, FakeRedis, FakeServer
from trading_core import WrongEnvironment, environment_from
from trading_core.environment import REDIS_KEY, check_folder, check_redis, check_redis_async


def test_trading_env_defaults_to_development_and_is_checked():
    assert environment_from({}) == "development"
    assert environment_from({"TRADING_ENV": " Production "}) == "production"
    with pytest.raises(ValueError, match="TRADING_ENV must be one of"):
        environment_from({"TRADING_ENV": "prod"})


def test_a_state_folder_is_marked_by_the_first_environment_to_use_it(tmp_path):
    folder = tmp_path / "dc"
    check_folder(folder, "beta")
    assert (folder / "environment").read_text().strip() == "beta"
    check_folder(folder, "beta")  # its own: fine
    with pytest.raises(WrongEnvironment, match="belongs to beta, but this is production"):
        check_folder(folder, "production")


def test_production_marks_only_an_empty_state_folder(tmp_path):
    used = tmp_path / "used"
    used.mkdir()
    (used / "seq.json").write_text("{}")
    with pytest.raises(WrongEnvironment, match="already holds data"):
        check_folder(used, "production")
    check_folder(used, "development")  # development and beta adopt what they find (from before the marks)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "lock").write_text("123")  # the lock is taken before the check
    check_folder(fresh, "production")
    assert (fresh / "environment").read_text().strip() == "production"


def test_redis_is_marked_and_another_environment_refused():
    redis = FakeRedis(server=FakeServer())
    redis.set("md:last:X", "{}")
    check_redis(redis, "development")
    assert redis.get(REDIS_KEY) == b"development"
    with pytest.raises(WrongEnvironment, match="Redis database 0 belongs to development"):
        check_redis(redis, "production")


def test_production_marks_only_an_empty_redis_database():
    async def go():
        used = FakeAsyncRedis(server=FakeServer())
        await used.set("orders", "x")
        with pytest.raises(WrongEnvironment, match="already holds data"):
            await check_redis_async(used, "production")
        empty = FakeAsyncRedis(server=FakeServer(), db=2)
        await check_redis_async(empty, "production")
        await empty.set("orders", "x")
        await check_redis_async(empty, "production")  # marked: its own data is fine
        with pytest.raises(WrongEnvironment, match="belongs to production, but this is beta"):
            await check_redis_async(empty, "beta")

    asyncio.run(go())
