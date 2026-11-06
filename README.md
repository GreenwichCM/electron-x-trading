# ElectronX trading platform

Trading UI, risk dashboard and FIX services for ElectronX. Architecture:
https://claude.ai/artifact/K8bLZDVaUUFQRhkVMpqCSw

## Layout

| Path | What |
|---|---|
| `packages/core` | Shared definitions: markets, products, symbols |
| `packages/fix` | FIX session layer (logon, heartbeats, sequence numbers, resend, mutual TLS) |
| `packages/sim` | Simulated ElectronX gateways for tests and development |
| `packages/db` | SQL Server settings, connections and schema migrations |
| `services/marketdata` | MD session -> order books -> Redis |
| `services/orderentry` | Pre-trade risk, throttle, OE session, internal order endpoint, kill switch |
| `services/dropcopy` | The exchange's copy of every fill: positions, break detection, kill switch trip |
| `services/riskmonitor` | Marks, P&L and limit usage per trader; blocks a trader who hits the loss limit |
| `services/recorder` | Copies orders, fills and alerts from Redis into SQL Server, permanently |
| `services/api` | FastAPI: login, permissions, REST and WebSockets for the web app |
| `web` | React + TypeScript trading UI |

## First-time setup (WSL)

Copy each `.env.example` to `.env` next to it and fill in the secrets (see the comments in each file).
`services/api/.env` and `services/orderentry/.env` must have the same `OE_INTERNAL_TOKEN`;
`services/recorder/.env` has the same `DB_PASSWORD` as `MSSQL_SA_PASSWORD` in `.env`.

Only one copy of orderentry and of dropcopy may run at a time; a second one refuses to start.

    uv sync
    npm --prefix web install
    docker compose up -d
    uv run --env-file services/recorder/.env python -m trading_db migrate

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

## History in SQL Server

The recorder copies every order update, fill and alert into SQL Server. Redis keeps only the latest
entries; SQL Server keeps everything. All tables and views are in their own `electronx` schema
(`electronx.orders`, `electronx.fills`, ...), so the database can be shared with other
applications. Schema changes are numbered files in `packages/db/src/trading_db/migrations`, applied
by the `migrate` command above (`status` lists them).

    docker compose exec sqlserver /opt/mssql-tools18/bin/sqlcmd -C -U sa -P "$(grep MSSQL_SA_PASSWORD .env | cut -d= -f2-)" \
        -d trading -Q "SELECT TOP 10 f.exec_time, o.trader, f.symbol, f.side, f.qty, f.price FROM electronx.fills AS f LEFT JOIN electronx.orders AS o ON o.cl_ord_id = f.cl_ord_id ORDER BY f.exec_time DESC"

Fills come from drop copy and carry the ClOrdID, not the trader; the join with `electronx.orders` adds it.

To use another SQL Server (for example a shared company database), point `DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER` and `DB_PASSWORD` in `services/recorder/.env` at it and run `migrate`. For a
login that can reach only the `electronx` schema, give your DBA `packages/db/dba-setup.sql`.

## Start again from a clean slate

Positions, sequence numbers and order history live in `var/` and in Redis. To wipe them all
(development only), stop everything, then:

    rm -rf var
    docker compose exec redis redis-cli del orders fills alerts killswitch dc:positions risk:snapshot risk:blocked

This keeps the history in SQL Server. To wipe that too: `docker compose down -v`, `docker compose up -d`,
then run `migrate` again.

## Tests

    uv run pytest
    uv run ruff check && uv run ruff format --check
    npm --prefix web run build

Tests that need SQL Server use the one from `docker compose up -d` (each in its own temporary
database) and are skipped when it is not running.
