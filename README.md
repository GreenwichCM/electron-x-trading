# ElectronX trading platform

Trading UI, risk dashboard and FIX services for ElectronX. Architecture:
https://claude.ai/artifact/K8bLZDVaUUFQRhkVMpqCSw

## Layout

| Path | What |
|---|---|
| `packages/core` | Shared definitions: markets, products, symbols |
| `packages/fix` | FIX session layer (logon, heartbeats, sequence numbers, resend, mutual TLS) |
| `packages/sim` | Simulated ElectronX gateways for tests and development |
| `services/marketdata` | MD session -> order books -> Redis |
| `services/orderentry` | Pre-trade risk, throttle, OE session, internal order endpoint |
| `services/api` | FastAPI: login, permissions, REST and WebSockets for the web app |
| `web` | React + TypeScript trading UI |

## First-time setup (WSL)

Copy each `.env.example` to `.env` next to it and fill in the secrets (see the comments in each file).
`services/api/.env` and `services/orderentry/.env` must have the same `OE_INTERNAL_TOKEN`.

    uv sync
    npm --prefix web install
    docker compose up -d redis

## Run everything

    uv run honcho start

Then open http://localhost:5173 and sign in as a test user. Ctrl-C stops all services.
To run only some: `uv run honcho start sim marketdata`.

## Tests

    uv run pytest
    uv run ruff check && uv run ruff format --check
    npm --prefix web run build
