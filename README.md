# ElectronX trading platform

Trading UI, risk dashboard and FIX services for ElectronX. Architecture:
https://claude.ai/artifact/K8bLZDVaUUFQRhkVMpqCSw

## Layout

| Path | What |
|---|---|
| `packages/core` | Shared definitions: markets, products, symbols |
| `packages/fix` | FIX session layer (logon, heartbeats, sequence numbers, resend, mutual TLS) |
| `packages/sim` | Simulated ElectronX gateways for tests and development |
| `packages/db` | SQL Server settings, connections, schema migrations and trading rules |
| `services/marketdata` | MD session -> order books -> Redis |
| `services/orderentry` | Pre-trade risk, throttle, OE session, internal order endpoint, kill switch |
| `services/dropcopy` | The exchange's copy of every fill: positions, break detection, kill switch trip |
| `services/riskmonitor` | Marks, daily P&L and limit usage per trader; blocks a trader who hits the daily loss limit |
| `services/recorder` | Copies orders, fills and alerts from Redis into SQL Server, permanently |
| `services/api` | FastAPI: login, permissions, REST and WebSockets for the web app |
| `web` | React + TypeScript trading UI |

## First-time setup (WSL)

Copy each `.env.example` to `.env` next to it and fill in the secrets (see the comments in each file).
`services/api/.env` and `services/orderentry/.env` must have the same `OE_INTERNAL_TOKEN`.
The database login every service shares is in `services/db.env` (from `services/db.env.example`); with
the development SQL Server its `DB_PASSWORD` is `MSSQL_SA_PASSWORD` from `.env`.

Only one copy of orderentry and of dropcopy may run at a time; a second one refuses to start.

    uv sync
    npm --prefix web install
    docker compose up -d
    uv run --env-file services/db.env python -m trading_db migrate
    uv run --env-file services/db.env python -m trading_db seed-dev    # rules for the test users

## Run everything

    uv run honcho start

Then open http://localhost:5173 and sign in as a test user: alice and dave (traders), bob and erin
(risk), carol (management). Ctrl-C stops all services.
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
`DB_NAME`, `DB_USER` and `DB_PASSWORD` in `services/db.env` at it and run `migrate`. For a
login that can reach only the `electronx` schema, give your DBA `packages/db/dba-setup.sql`.

## Trading rules

Who may trade what, and the limits, live in SQL Server, and orderentry and riskmonitor reload them
every few seconds, so a change takes effect without a restart:

| Table | What |
|---|---|
| `electronx.traders` | Who may trade (`active`), and their ElectronX trader id for SenderSubID(50) |
| `electronx.entitlements` | What each trader may trade, by market and product (`'*'` = any) |
| `electronx.limits` | Order size and value, open orders, position per contract, max loss |

Entitlements and limits are keyed by trader, market and product, where `'*'` means any; for each
limit the most specific row wins, and the `('*', '*', '*')` row is the desk default. The rules fail
closed: anyone not in `traders`, a contract no entitlement covers, or a limit no row sets means the
order is rejected. If SQL Server is unreachable the last rules loaded stay in force. Cancels are never
blocked.

Change them on the Trading rules page: a risk user proposes a change with a reason, and a second risk
user approves it (never the same person). Approving applies it, and `electronx.rule_changes` keeps
every change with the rule before and after, who asked, who decided and when. A proposal whose rule
changed in the meantime becomes `stale` instead of overwriting the newer rule. Management sees the
rules and the history; each trader sees the rules that apply to them.

The same tables can be edited directly in SSMS (not audited; in production, only the app's login
should be able to write them). Examples:

    -- let alice trade PJM too
    INSERT electronx.entitlements (trader_id, market, product) VALUES (N'alice', 'PJM', '*');
    -- at most 5 lots per contract for dave in PJM BFUT100
    INSERT electronx.limits (trader_id, market, product, max_position) VALUES (N'dave', 'PJM', 'BFUT100', 5);
    -- stop dave trading (his live orders stay; use Block on the dashboard to cancel them too)
    UPDATE electronx.traders SET active = 0 WHERE trader_id = N'dave';

## Daily P&L

The risk dashboard shows P&L for today's trading day, and since riskmonitor's first fill. ElectronX
has no trading day of its own, so it is set in `services/riskmonitor/.env`: by default midnight to
midnight New York time (`RISK_DAY_TZ=America/New_York`, `RISK_DAY_START=00:00`). The max loss in
`electronx.limits` is a daily limit: it is checked against today's P&L. A block stays until risk lifts
it, also across the day roll.

Positions held when the day begins are valued from their start-of-day mark, the contract's mark at
that moment. Riskmonitor keeps these marks in Redis (`risk:sod`), so a restart during the day keeps
them. If riskmonitor was down at the roll, the first mark after it starts stands in, shown as late.

A contract stops trading at the last trade time the exchange sends in its SecurityList, or else at the
end of its hour. A symbol's date and hour are in the ISO's own time zone: Central for ERCOT and SPP,
Eastern for PJM, NYISO and ISO-NE, Pacific for CAISO, and EST all year for MISO; the repeated hour
when clocks fall back is `X2`. From then on its book is ignored and it keeps its last mark, shown as
awaiting settlement, until the exchange sends the settlement price on market data (MDEntryType 6).
The position is then closed at that price: its P&L becomes realized and it no longer counts against
the position limit. An indicative settlement price is only used as the mark.

The exchange keeps listing new hourly contracts, so marketdata asks for the instrument list again every
5 minutes (`MD_LIST_REFRESH_S`) and subscribes to the new ones. A contract that leaves the list is
still followed until its settlement price arrives, or for 3 days (`MD_KEEP_UNSETTLED_S`); then its
Redis keys expire after 30 days. On the Positions check, settled contracts are left out once both
sides agree on them.

With `SNAPSHOT_URL` and `SNAPSHOT_API_KEY` set in `services/marketdata/.env`, marketdata also asks the
ElectronX Snapshot API every minute for the exchange's marks and settlement prices. A Bounded Future
whose book is too wide or empty is then marked at the exchange's day-ahead price, kept inside the bid
and ask; never a BOPT, whose day-ahead price is its strike. A settlement price from the Snapshot API
closes a position just like one over FIX; FIX comes first, and if the two differ by more than a cent
marketdata logs a warning. The simulator has no Snapshot API, so leave both empty in development. To
check a key: `uv run --env-file services/marketdata/.env python -m trading_marketdata.snapshot ERCOT`.

To try this with the simulator, `SIM_EXPIRING_IN_S=300 uv run honcho start` adds an ERCOT Houston
contract that stops trading 5 minutes after the start and settles 30 seconds later
(`SIM_SETTLE_AFTER_S`).

## Start again from a clean slate

Positions, sequence numbers and order history live in `var/` and in Redis. To wipe them all
(development only), stop everything, then:

    rm -rf var
    docker compose exec redis redis-cli del orders fills alerts killswitch dc:positions risk:snapshot risk:blocked risk:sod

This keeps the history in SQL Server. To wipe that too: `docker compose down -v`, `docker compose up -d`,
then run `migrate` again.

## Tests

    uv run pytest
    uv run ruff check && uv run ruff format --check
    npm --prefix web run build

Tests that need SQL Server use the one from `docker compose up -d` (each in its own temporary
database) and are skipped when it is not running.
