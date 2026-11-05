"""Run the api: `uv run --env-file services/api/.env python -m trading_api`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .main import create_app
from .settings import ConfigError, Settings


def main() -> None:
    try:
        settings = Settings.from_env()
        app = create_app(settings=settings)
    except ConfigError as exc:
        sys.exit(f"config error: {exc}")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if settings.dev_login:
        logging.getLogger("api").warning("DEVELOPMENT LOGIN IS ON: anyone who can reach this api can sign in as anyone")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
