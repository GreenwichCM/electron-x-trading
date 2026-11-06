"""Run the recorder: `uv run --env-file services/recorder/.env python -m trading_recorder`."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from redis import Redis
from trading_db import ConfigError, connect

from .recorder import Recorder, SchemaError
from .settings import Settings

log = logging.getLogger("recorder")


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    redis = Redis.from_url(settings.redis_url)
    recorder = Recorder(redis, lambda: connect(settings.db, appname="recorder"), batch=settings.batch)
    log.info("recording orders, fills and alerts into %r", settings.db)
    try:
        recorder.run(stop)
    except SchemaError as exc:
        sys.exit(str(exc))
    finally:
        redis.close()
    log.info("stopped after recording %d entries", recorder.recorded)


if __name__ == "__main__":
    main()
