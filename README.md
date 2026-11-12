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

There are three environments, each with its own Redis database, state folders and SQL database:
development (the simulator; `Procfile`), beta (ElectronX beta, beta.electronx.com; `Procfile.uat`) and
production (trade.electronx.com; `Procfile.prod`). `TRADING_ENV` says which one a service belongs to
(development when not set). The first service to use a Redis database, state folder or SQL database marks
it as its environment's, and services of another environment refuse it at startup with "wrong
environment: ... belongs to beta, but this is production": the settings point at the wrong one.

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

By default the simulator lists three contracts that never expire. `SIM_UNIVERSE=full uv run honcho start`
lists what the exchange does instead: every hour of today and tomorrow for each ERCOT hub and PJM Western
Hub, in BFUT100, BFUT500, BFUT5000 and BOPT (about 1,800 contracts), each stopping trading when its hour
begins and settling after it ends, with new days listed as they come.

The price screen shows at most 200 contracts, and the browser gets live prices only for those:

* Filters: market, hub, BFUT or BOPT, product size, delivery day, hours ending, peak or off-peak (weekdays,
  HE07-22 Central and Pacific, HE08-23 Eastern; holidays not taken out), closing within, only with prices
  (a bid and an offer now), my contracts (a position or a working order), hide ended (expired, settled),
  and a search ("north 18").
* Each row shows its state: OPEN, CLOSED, HALTED, ENDED, SETTLED, or NO DATA when no live book has arrived.
  A line per market says how many contracts are open, closed or not reported yet, so a closed market reads
  as closed.
* Columns can be chosen (spread, mid, last trade time, closes in, day-ahead, settlement, position, working
  orders) and the rows shown sorted by clicking a header. The strip view shows one market, day and product
  as hours down and hubs across.
* Clicking a price fills in the order ticket; clicking a row shows its order book, every level, where a
  click on a price does the same. The ticket checks the contract's tick size, lot size, price limits and
  state before sending.
* Filters, columns, layout and sort are remembered per user in their browser, and can be saved as named views.

Marketdata subscribes at the exchange in order of interest, as trading platforms do: first the contracts
on someone's screen (the api tells it through Redis, `md:wanted:*`), then those the account holds, then
every other contract still trading, soonest first (`MD_BACKGROUND=N` leaves that last part out). A
contract whose trading has ended is followed only while it is on a screen or held. So the screen has live
prices within seconds of startup, even with tens of thousands of contracts listed.

## Restarts

Any service can be stopped and started again on its own. orderentry saves its orders and positions
to `var/oe/orders.json` every second and replays the rest from the Redis `orders` stream, then the
exchange resends whatever happened while it was down. Orders still live from before a weekly
sequence reset (Sunday 06:15 UTC) are cancelled, because what happened to them cannot be confirmed.
The simulator keeps resting orders across its own restarts, like the exchange.

## Orders from the ElectronX screen

Traders can also trade on the exchange's own screen. The drop copy reports those orders too, each with
the trader's ElectronX id (TargetSubID 57), so they are not lost:

* dropcopy counts an order as ours if orderentry sent it, and otherwise, after 2 seconds, as from the
  ElectronX screen. Only our orders are compared with orderentry, so screen trading never trips the kill
  switch. The Positions panel shows it in its own column; a fill not placed yet shows as "checking".
* A user's id is their ElectronX login (see Users and accounts below), so a screen order belongs to the
  user whose id is its TargetSubID, whatever its case (`Jane@x.com` is `jane@x.com`). riskmonitor counts
  their screen fills in their P&L and daily loss limit, and orderentry against their position limit.
* The dashboard warns risk about anyone trading on the screen whose ElectronX login is not a user here.
  Their orders are still recorded, with `trader` blank. Adding them as a user on the Trading rules page
  then fills them in on those orders as well. After users are added directly in SQL:

      uv run --env-file services/db.env python -m trading_db check-traders --fill-in

  Without `--fill-in` it only lists who is missing (exit code 1 if anyone is), for a daily check.
* Orders the exchange rejects outright ("Book is CLOSED") never get an OrderID; they are recorded too,
  by their ClOrdID, with status `rejected`.
* The recorder keeps screen orders in `electronx.orders` and `electronx.order_events` with
  `source = 'electronx'` and the ElectronX id in `exchange_user`; every fill has `exchange_user` too. Our
  own orders have `source = 'app'`.
* The order blotter shows them next to ours, marked ElectronX, to the trader they belong to (risk and
  management see all). They can be cancelled only on the ElectronX screen. The other way round needs
  nothing from us: orders sent from here are on the same trading account, so the ElectronX screen
  shows them.

To try it, have the simulator place screen orders as dave, and as someone who is not a user here:

    SIM_SCREEN_USERS=dave@example.test,stranger@example.test uv run honcho start

`SIM_SCREEN_EVERY_S` (60) sets how often.

## ElectronX demo (UAT)

The same services connect to the ElectronX demo environment, beta (beta.electronx.com), instead of the
simulator. `Procfile.uat`
starts them without the simulator. Each service loads its usual `.env`, then `services/uat.env` (its own
Redis database and SQL database, `trading_uat`, so nothing mixes with the simulator's), then its own
`uat.env` with the exchange's address, passwords and certificates. All of these and the certificates are
git-ignored: never commit them. Stop the development stack first; both use the api and web ports.

1. Certificates: put the three files ElectronX sent in `certs/` (`ca.crt`, `client.crt`, `client.key`),
   then `chmod 600 certs/client.key`.
2. Settings: copy `services/uat.env.example` to `services/uat.env`, and in `services/marketdata`,
   `services/orderentry` and `services/dropcopy` copy `uat.env.example` to `uat.env`. Fill in the three
   passwords from the ElectronX FIX connectivity sheet. `services/uat.env` says `TRADING_ENV=beta`.
3. Check the way to the exchange without logging on (no FIX password is used):

       uv run --env-file services/marketdata/.env --env-file services/marketdata/uat.env python -m trading_fix.check MD
       uv run --env-file services/orderentry/.env --env-file services/orderentry/uat.env python -m trading_fix.check OE
       uv run --env-file services/dropcopy/.env --env-file services/dropcopy/uat.env python -m trading_fix.check DC

   A TCP failure means ElectronX has not allowed this machine's public IP address yet, or a firewall
   blocks the port. A TLS failure means the certificate files are not the right ones.
4. Database: `migrate` with `--env-file services/db.env --env-file services/uat.env`. Then add the
   sheet's Authorized Trading Users as users (their email is their id, role trader) and its account to
   `electronx.accounts`, and let them trade on it (see Users and accounts). `seed-dev` adds the test
   users too, if you want to sign in as a risk user to approve changes.
5. Read only first: `uv run honcho -f Procfile.uat start marketdata dropcopy riskmonitor recorder api web`.
   Prices fill the price screen; demo books are sometimes CLOSED, so use "Trading now".
6. Then everything: `uv run honcho -f Procfile.uat start`. Place a one-lot order far from the market, see
   it acknowledged, cancel it.
7. On the ElectronX demo screen, place a one-lot order far from the market, then cancel it. It must appear
   in the blotter marked ElectronX, as the trader with that email, first as new, then as cancelled.

ElectronX allows one connection per SenderCompID, and keeps sequence numbers all week (they reset on
Sunday at 06:15 UTC); orderentry and dropcopy keep theirs in `var/uat`. Marketdata subscribes to at most
`MD_SUBSCRIBE_PER_S` (50) contracts a second, those on a screen first, so they have prices within seconds;
the rest of the 30,000 or so contracts the demo lists follow in the background.

For production, ElectronX reviews the order entry session's activity in the demo environment against
minimum message counts (logons, heartbeats, resend requests, rejects, orders, cancels). They were met on
21 October 2026 by the earlier client (electron-x-oms, `exoms conformance`).

## Production (record-only)

`Procfile.prod` connects to ElectronX production (trade.electronx.com) and records; it sends nothing.
Traders enter their orders on the ElectronX screen. The drop copy reports every order and fill of the
account, and the recorder keeps them in the production database, POWER (`services/db.power.env`), with
`source = 'electronx'` and the trader's ElectronX login, as in "Orders from the ElectronX screen" above.
Market data, positions, P&L and the risk pages work as in beta.

There is no order entry session. orderentry refuses to run with `TRADING_ENV=production`, the api runs
with `ORDER_ENTRY=N` (`services/prod.env`), and the app shows no order ticket, kill switch or trader
blocks: they only ever stop orders sent from the app. Sending orders from the app waits for a real
sign-in (Google, step 14). Until then, anyone who can open the app on this machine can pick any user on
the development sign-in page, which is fine for looking, not for trading.

Production keeps everything apart from development and beta: Redis database 2, `var/prod`, `certs/prod`
and POWER. It also only marks an empty Redis database, state folder or SQL database as its own: one that
already has data without a mark was used by development or beta, so it is refused rather than mixed in.
Production also refuses `MD_TLS=N` or `DC_TLS=N`, a host on this machine (the simulator) and the beta
Snapshot API; beta refuses the production Snapshot API.

1. Certificates: put the three production files from ElectronX in `certs/prod/` (`ca.crt`, `client.crt`,
   `client.key`), then `chmod 600 certs/prod/client.key`. The beta ones stay in `certs/`.
2. Settings: copy `services/prod.env.example` to `services/prod.env`, `services/db.power.env.example` to
   `services/db.power.env` (the production database login), and in `services/marketdata` and
   `services/dropcopy` copy `prod.env.example` to `prod.env`. Fill in the host, port, SenderCompID and
   password of each session from the production FIX connectivity sheet. All are git-ignored.
3. Check the way to the exchange without logging on:

       uv run --env-file services/marketdata/.env --env-file services/prod.env --env-file services/marketdata/prod.env python -m trading_fix.check MD
       uv run --env-file services/dropcopy/.env --env-file services/prod.env --env-file services/dropcopy/prod.env python -m trading_fix.check DC

4. Database. Bring POWER's schema up to date, then look at what it holds:

       uv run --env-file services/db.power.env --env-file services/prod.env python -m trading_db migrate
       uv run --env-file services/db.power.env python -m trading_db clear-data

   POWER still has rows from development tests. Once you have checked they are only test rows, delete them
   (this asks for the database's name; it refuses a database marked as production's):

       uv run --env-file services/db.power.env python -m trading_db clear-data --confirm POWER

5. The first users. Every later change goes through the Trading rules page, proposed by one person and
   approved by another with the risk role, so add at least two people with risk directly:

       uv run --env-file services/db.power.env --env-file services/prod.env python -m trading_db add-user jane@greenwichcm.com --roles trader,risk --accounts TA-HURON-00001

   Their email is their ElectronX login and their id. A missing account is added too (capacity 2).
   `add-user` stops working once two active users have the risk role: add everyone else on the page. The
   first one also marks POWER as production's, after which development and beta refuse it.
6. Start: `uv run honcho -f Procfile.prod start` (stop the development and beta stacks first). The app's
   header says "ElectronX PRODUCTION · record-only". A trader's order on the ElectronX screen shows in the
   blotter, marked ElectronX, as theirs.
7. Daily: anyone who traded on the ElectronX screen without being a user here (their orders are recorded
   with no trader until they are added):

       uv run --env-file services/db.power.env --env-file services/prod.env python -m trading_db check-traders

Before relying on it: run it on a machine that stays on (a laptop that sleeps misses the drop copy until
it catches up on reconnecting), and check POWER's server certificate (`connect()` encrypts the connection
but does not verify the certificate yet; on one machine that was fine, across the internet it is not).

## History in SQL Server

The recorder copies every order update, fill and alert into SQL Server. Redis keeps only the latest
entries; SQL Server keeps everything. All tables and views are in their own `electronx` schema
(`electronx.orders`, `electronx.fills`, ...), so the database can be shared with other
applications. Schema changes are numbered files in `packages/db/src/trading_db/migrations`, applied
by the `migrate` command above (`status` lists them).

    docker compose exec sqlserver /opt/mssql-tools18/bin/sqlcmd -C -U sa -P "$(grep MSSQL_SA_PASSWORD .env | cut -d= -f2-)" \
        -d trading -Q "SELECT TOP 10 f.exec_time, o.trader, f.symbol, f.side, f.qty, f.price FROM electronx.fills AS f LEFT JOIN electronx.orders AS o ON o.cl_ord_id = f.cl_ord_id WHERE f.busted_at IS NULL ORDER BY f.exec_time DESC"

Fills come from drop copy and carry the ClOrdID, not the trader; the join with `electronx.orders` adds it.
A fill the exchange cancels afterwards (a trade bust) stays in the table with `busted_at`, `bust_exec_id`
and `bust_reason` set: leave those out (`WHERE busted_at IS NULL`) to count the trades that stand.

`electronx.orders` has one row per order, its latest state (what the blotter shows); `electronx.order_events`
has a row for every change, so an order's whole life can be read back. Each event says:

* what happened (`event`): `new_order`, `refused` (by our checks, never sent), `cancel_request`,
  `cancel_refused` (not your order, already closed, not connected), `exchange_report`, `trade_bust` (the
  exchange cancelled one of the order's fills afterwards: `exec_ref_id` names it, and `busted_qty` adds
  them up), `cancel_reject`, `business_reject`, `session_reject` (the exchange's FIX session refused
  the message as malformed, so it never reached the book)
* who or what caused it (`by_user`): the trader, the risk user who tripped the kill switch or blocked the
  trader (or riskmonitor, dropcopy), orderentry after a restart, the trader on the ElectronX screen, or
  `exchange`; why (`reason`); and when, here (`event_at`) and at the exchange (`exchange_time`)
* the exchange's report: `exec_id`, `exec_type`, the fill (`last_qty`, `last_px`, `trade_id`, `aggressor`,
  `commission`), `leaves_qty`, `reject_code`, `restatement_reason` (8 = cancelled on disconnect), and
  `request_id`, the ClOrdID of the cancel request a report answers
* what the order was sent with: `account`, `exchange_user` (SenderSubID), `capacity`, `ord_type`, `manual`

    SELECT version, event, by_user, status, exec_type, last_qty, last_px, reason, event_at, exchange_time
    FROM electronx.order_events WHERE cl_ord_id = '<ClOrdID>' ORDER BY version

Nothing in the app writes orders to the database: an action goes through orderentry and FIX to the
exchange, and the exchange's reports decide each order's state; the recorder only copies the events. The
exchange reports our orders twice, on orderentry's session and on the drop copy, and
`electronx.exchange_reports` keeps every report the drop copy received (ours and the screen's) as
received, with the whole FIX message in `raw`. `reconcile` compares the two, report by report, and lists
any report only one side has (it exits with 1 if there is one):

    uv run --env-file services/db.env python -m trading_db reconcile --hours 24

### Trade busts

ElectronX can cancel a trade after it happened (a bust): it sends an ExecutionReport with ExecType `H`
for each side, naming the cancelled fill in ExecRefID. Everything takes the fill back out: drop copy's
positions, orderentry's per trader (so the position limit frees up), riskmonitor's position and P&L
(as if the trade had never happened), and the screen's. The order keeps its status and `cum_qty`, as the
exchange reports them, with `busted_qty` beside them; the blotter shows "(1 busted)". In SQL the fill
is marked (`busted_at`), the order gets a `trade_bust` event, and the report is in
`electronx.exchange_reports` with `exec_type` H.

To try it with the simulator, trade something, then bust the latest trade (or name its TrdMatchID):

    echo last > var/sim/bust

To use another SQL Server (for example a shared company database), point `DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER` and `DB_PASSWORD` in `services/db.env` at it and run `migrate`. For a
login that can reach only the `electronx` schema, give your DBA `packages/db/dba-setup.sql`.

## Trading rules

Who may trade what, and the limits, live in SQL Server, and orderentry and riskmonitor reload them
every few seconds, so a change takes effect without a restart:

| Table | What |
|---|---|
| `electronx.users` | Everyone with a login: their ElectronX login (company email) is their id |
| `electronx.user_roles` | What each does: `trader` (may place orders), `risk`, `management` |
| `electronx.accounts` | The ElectronX trading accounts (Account, tag 1) and the capacity sent on them |
| `electronx.account_access` | Which accounts each trader may trade on |
| `electronx.entitlements` | What each trader may trade, by market and product (`'*'` = any) |
| `electronx.limits` | Order size and value, open orders, position per contract, max loss |

Entitlements and limits are keyed by trader, market and product, where `'*'` means any; for each
limit the most specific row wins, and the `('*', '*', '*')` row is the desk default. The rules fail
closed: anyone not an active user with the trader role and an account, a contract no entitlement
covers, or a limit no row sets means the
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
    INSERT electronx.entitlements (trader_id, market, product) VALUES (N'alice@example.test', 'PJM', '*');
    -- at most 5 lots per contract for dave in PJM BFUT100
    INSERT electronx.limits (trader_id, market, product, max_position)
        VALUES (N'dave@example.test', 'PJM', 'BFUT100', 5);
    -- stop dave trading (his live orders stay; use Block on the dashboard to cancel them too)
    UPDATE electronx.users SET active = 0 WHERE user_id = N'dave@example.test';

### Users and accounts

A person is known here the way ElectronX knows them: by their ElectronX login, their company email in
lower case. It is their id in every table, on the sign-in page, and the SenderSubID(50) on every order
sent for them; ElectronX's reports name them by it (TargetSubID 57), on orders from the ElectronX screen
too. There is no shared trader id: someone who is not an active user with the trader role cannot send
an order. Each order goes on an account in `electronx.accounts` they may trade on; with one, that one.

Add and change users on the Trading rules page ("Propose a new user"), with the same two-person
approval as the rules. In SQL, for example a new trader on the demo account:

    INSERT electronx.users (user_id, display_name) VALUES (N'jane@company.com', N'Jane');
    INSERT electronx.user_roles (user_id, role) VALUES (N'jane@company.com', 'trader');
    INSERT electronx.account_access (user_id, account_id) VALUES (N'jane@company.com', 'TA-HURON-00001');

and an account: `INSERT electronx.accounts (account_id, name, capacity) VALUES ('TA-...', N'...', '2')`.
`seed-dev` adds the test users (alice and dave trade, bob and erin are risk, carol is management, all
`@example.test`) and the simulator's account. Users sign in with the development login until Google
sign-in arrives; it lists the active users.

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
    docker compose exec redis redis-cli del orders fills alerts screen_orders killswitch dc:positions risk:snapshot risk:blocked risk:sod

For the ElectronX demo, the same with `redis-cli -n 1` and `rm -rf var/uat`; but at the exchange orders
and sequence numbers live on, so only do this after the weekly reset.

This keeps the history in SQL Server. To wipe that too: `docker compose down -v`, `docker compose up -d`,
then run `migrate` again.

## Tests

    uv run pytest
    uv run ruff check && uv run ruff format --check
    npm --prefix web run build

Tests that need SQL Server use the one from `docker compose up -d` (each in its own temporary
database) and are skipped when it is not running.
