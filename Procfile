# Development: `uv run honcho start` runs everything in one terminal; Ctrl-C stops it all.
# Redis runs in Docker separately: `docker compose up -d redis` (it stays up between sessions).
sim: uv run python -m trading_sim
marketdata: uv run --env-file services/marketdata/.env python -m trading_marketdata
orderentry: uv run --env-file services/orderentry/.env python -m trading_orderentry
api: uv run --env-file services/api/.env python -m trading_api
web: npm --prefix web run dev
