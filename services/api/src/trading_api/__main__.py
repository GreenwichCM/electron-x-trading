"""Run the api: `uv run --env-file services/api/.env python -m trading_api`."""

from __future__ import annotations

import logging

import uvicorn

from .main import create_app
from .settings import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
