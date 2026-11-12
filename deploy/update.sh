#!/usr/bin/env bash
# After new code arrives on the always-on machine (git pull): bring the Python packages, the database schema and
# the web app up to date, then restart production. `bash deploy/update.sh`
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync
uv run --env-file services/db.power.env --env-file services/prod.env python -m trading_db migrate
npm --prefix web ci --silent
npm --prefix web run build
sudo bash deploy/install-services.sh # in case Procfile.prod changed
sudo systemctl restart trading.target
sleep 10
uv run python deploy/status.py
