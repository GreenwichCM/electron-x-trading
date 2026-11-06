-- Order, fill and alert history, copied from the Redis streams by the recorder service.
-- Times are UTC. Prices are USD/MWh, quantities are lots.

-- How far the recorder has got in each Redis stream (written in the same transaction as the rows).
CREATE TABLE dbo.stream_offsets (
    stream     varchar(64)  NOT NULL PRIMARY KEY,
    last_id    varchar(32)  NOT NULL,  -- Redis entry id, e.g. 1762397762069-0
    updated_at datetime2(3) NOT NULL DEFAULT sysutcdatetime()
);

-- Every update orderentry published: the whole life of each order.
CREATE TABLE dbo.order_events (
    stream_id   varchar(32)   NOT NULL PRIMARY KEY,
    cl_ord_id   varchar(64)   NOT NULL,
    version     int           NOT NULL,
    trader      nvarchar(128) NOT NULL,
    symbol      varchar(64)   NOT NULL,
    side        varchar(4)    NOT NULL,
    qty         int           NOT NULL,
    price       decimal(18,6) NOT NULL,
    tif         varchar(4)    NOT NULL,
    status      varchar(20)   NOT NULL,
    order_id    varchar(64)   NOT NULL,  -- the exchange's OrderID(37), empty until acknowledged
    cum_qty     int           NOT NULL,
    avg_px      decimal(18,6) NOT NULL,
    message     nvarchar(400) NOT NULL,  -- reject or cancel reason, FIX Text(58)
    created_at  datetime2(3)  NULL,
    updated_at  datetime2(3)  NULL,
    recorded_at datetime2(3)  NOT NULL DEFAULT sysutcdatetime()
);
CREATE INDEX ix_order_events_order ON dbo.order_events (cl_ord_id, version);

-- The latest state of each order (its highest version).
CREATE TABLE dbo.orders (
    cl_ord_id      varchar(64)   NOT NULL PRIMARY KEY,
    version        int           NOT NULL,
    trader         nvarchar(128) NOT NULL,
    symbol         varchar(64)   NOT NULL,
    side           varchar(4)    NOT NULL,
    qty            int           NOT NULL,
    price          decimal(18,6) NOT NULL,
    tif            varchar(4)    NOT NULL,
    status         varchar(20)   NOT NULL,
    order_id       varchar(64)   NOT NULL,
    cum_qty        int           NOT NULL,
    avg_px         decimal(18,6) NOT NULL,
    message        nvarchar(400) NOT NULL,
    created_at     datetime2(3)  NULL,
    updated_at     datetime2(3)  NULL,
    last_stream_id varchar(32)   NOT NULL
);
CREATE INDEX ix_orders_trader ON dbo.orders (trader, created_at);

-- The exchange's fills, from drop copy (they carry the ClOrdID, not the trader: see trader_fills).
CREATE TABLE dbo.fills (
    exec_id     varchar(64)   NOT NULL PRIMARY KEY,
    stream_id   varchar(32)   NOT NULL,
    order_id    varchar(64)   NOT NULL,
    cl_ord_id   varchar(64)   NOT NULL,
    account     varchar(64)   NOT NULL,
    symbol      varchar(64)   NOT NULL,
    side        varchar(4)    NOT NULL,
    qty         int           NOT NULL,
    price       decimal(18,6) NOT NULL,
    exec_time   datetime2(3)  NULL,  -- TransactTime(60)
    recorded_at datetime2(3)  NOT NULL DEFAULT sysutcdatetime()
);
CREATE INDEX ix_fills_symbol_time ON dbo.fills (symbol, exec_time);
CREATE INDEX ix_fills_order ON dbo.fills (cl_ord_id);

-- Kill switch changes and trader blocks.
CREATE TABLE dbo.alerts (
    stream_id   varchar(32)   NOT NULL PRIMARY KEY,
    type        varchar(40)   NOT NULL,  -- killswitch_on, killswitch_off, trader_blocked, trader_unblocked
    trader      nvarchar(128) NULL,      -- for trader blocks
    by_user     nvarchar(128) NOT NULL,  -- a user id, or the service that acted (dropcopy, riskmonitor)
    reason      nvarchar(400) NOT NULL,
    alert_time  datetime2(3)  NULL,
    data        nvarchar(max) NOT NULL,  -- the alert exactly as published (JSON)
    recorded_at datetime2(3)  NOT NULL DEFAULT sysutcdatetime()
);
CREATE INDEX ix_alerts_time ON dbo.alerts (alert_time);
GO

-- Fills with the trader who placed each order.
CREATE VIEW dbo.trader_fills AS
SELECT f.exec_id, f.exec_time, o.trader, f.symbol, f.side, f.qty, f.price, f.cl_ord_id, f.order_id, f.account
FROM dbo.fills AS f
LEFT JOIN dbo.orders AS o ON o.cl_ord_id = f.cl_ord_id;
