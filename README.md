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
| `services/orderentry` | Pre-trade risk, throttle, OE session, internal order endpoint, kill switch |
| `services/dropcopy` | The exchange's copy of every fill: positions, break detection, kill switch trip |
| `services/riskmonitor` | Marks, P&L and limit usage per trader; blocks a trader who hits the loss limit |
| `services/api` | FastAPI: login, permissions, REST and WebSockets for the web app |
| `web` | React + TypeScript trading UI |

## First-time setup (WSL)

Copy each `.env.example` to `.env` next to it and fill in the secrets (see the comments in each file).
`services/api/.env` and `services/orderentry/.env` must have the same `OE_INTERNAL_TOKEN`.

Only one copy of orderentry and of dropcopy may run at a time; a second one refuses to start.

    uv sync
    npm --prefix web install
    docker compose up -d redis

## Run everything

    uv run honcho start

Then open http://localhost:5173 and sign in as a test user. Ctrl-C stops all services.
To run only some: `uv run honcho start sim marketdata`.

## Restarts

Any service can be stopped and started again on its own. orderentry saves its orders and positions
to `var/oe/orders.json` every second and replays the rest from the Redis `orders` stream, then the
exchange resends whatever happened while it was down. Orders still live from before a weekly
sequence reset (Sunday 06:15 UTC) are cancelled, because what happened to them cannot be confirmed.
The simulator keeps resting orders across its own restarts, like the exchange.

## Start again from a clean slate

Positions, sequence numbers and order history live in `var/` and in Redis. To wipe them all
(development only), stop everything, then:

    rm -rf var
    docker compose exec redis redis-cli del orders fills alerts killswitch dc:positions risk:snapshot risk:blocked

## Tests

    uv run pytest
    uv run ruff check && uv run ruff format --check
    npm --prefix web run build
