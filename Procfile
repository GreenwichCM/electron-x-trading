# Development: `uv run honcho start` runs everything in one terminal; Ctrl-C stops it all.
# Redis and SQL Server run in Docker separately: `docker compose up -d` (they stay up between sessions).
# If any one process exits, honcho stops all of them: read the last lines to see which one and why.
sim: uv run python -m trading_sim
marketdata: uv run --env-file services/marketdata/.env python -m trading_marketdata
orderentry: uv run --env-file services/db.env --env-file services/orderentry/.env python -m trading_orderentry
dropcopy: uv run --env-file services/dropcopy/.env python -m trading_dropcopy
riskmonitor: uv run --env-file services/db.env --env-file services/riskmonitor/.env python -m trading_riskmonitor
recorder: uv run --env-file services/db.env --env-file services/recorder/.env python -m trading_recorder
api: uv run --env-file services/api/.env python -m trading_api
web: npm --prefix web run dev
