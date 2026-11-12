"""Which environment a process belongs to, and the checks that keep environments apart.

TRADING_ENV names it: `development` (the simulator; the default), `beta` (ElectronX beta,
beta.electronx.com) or `production` (trade.electronx.com). Each Redis database, state folder and SQL
database is marked with the environment that first uses it, and a process of another environment
refuses to use it: a beta service pointed at production's Redis, or a production service at the beta
database, stops at startup with a message saying so, instead of mixing the two.

Production is stricter: it only marks one that is empty. A Redis database, state folder or SQL database
that already holds data but has no mark (say, from before this check existed) was used by development
or beta, so production refuses it rather than adopt test data.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

ENVIRONMENTS = ("development", "beta", "production")
REDIS_KEY = "trading:environment"
FOLDER_MARKER = "environment"  # a file in each state folder
FOLDER_IGNORED = (FOLDER_MARKER, "lock")  # what an otherwise empty state folder may hold


class WrongEnvironment(Exception):
    pass


def environment_from(env: Mapping[str, str]) -> str:
    """TRADING_ENV, checked; ValueError if it is not one of ENVIRONMENTS."""
    name = env.get("TRADING_ENV", "").strip().lower() or "development"
    if name not in ENVIRONMENTS:
        raise ValueError(f"TRADING_ENV must be one of {', '.join(ENVIRONMENTS)} (it is {name!r})")
    return name


def decide(what: str, name: str, found: str | None, has_data: bool) -> bool:
    """Whether environment `name` should mark `what` now (True), or it is already marked as `name`'s
    (False). WrongEnvironment if it is another environment's, or if production would adopt data."""
    if found is not None:
        if found != name:
            raise WrongEnvironment(
                f"{what} belongs to {found}, but this is {name} (TRADING_ENV): the settings point at the "
                f"wrong one; {name} needs its own"
            )
        return False
    if name == "production" and has_data:
        raise WrongEnvironment(
            f"{what} already holds data but is not marked as production's: it was used by development or "
            "beta. Production only starts on an empty one (see Production in README.md)"
        )
    return True


def check_folder(folder: Path, name: str) -> None:
    """Mark a state folder (sequence numbers, saved orders, the fill journal) as `name`'s, or refuse it."""
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / FOLDER_MARKER
    found = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
    has_data = any(p.name not in FOLDER_IGNORED for p in folder.iterdir())
    if decide(f"state folder {folder}", name, found, has_data):
        marker.write_text(name + "\n", encoding="utf-8")


def _redis_name(redis: Any) -> str:
    kwargs = getattr(getattr(redis, "connection_pool", None), "connection_kwargs", {}) or {}
    return f"Redis database {kwargs.get('db', 0)}"


def _text(value: Any) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


def check_redis(redis: Any, name: str) -> None:
    """The same for a Redis database (a redis.Redis client)."""
    what = _redis_name(redis)
    found = _text(redis.get(REDIS_KEY))
    if decide(what, name, found, found is None and redis.dbsize() > 0):
        if not redis.set(REDIS_KEY, name, nx=True):  # another process marked it just now
            decide(what, name, _text(redis.get(REDIS_KEY)), False)


async def check_redis_async(redis: Any, name: str) -> None:
    """The same for a redis.asyncio.Redis client."""
    what = _redis_name(redis)
    found = _text(await redis.get(REDIS_KEY))
    if decide(what, name, found, found is None and await redis.dbsize() > 0):
        if not await redis.set(REDIS_KEY, name, nx=True):
            decide(what, name, _text(await redis.get(REDIS_KEY)), False)
