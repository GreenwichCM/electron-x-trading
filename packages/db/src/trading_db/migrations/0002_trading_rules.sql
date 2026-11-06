-- Who may trade what, and how much (see trading_core/rules.py for how the rows are read).
-- orderentry and riskmonitor reload these every few seconds: a change here takes effect without a restart.
-- '*' means any: any trader (the desk default), any market, any product.

-- Everyone who may trade. Someone not listed here, or not active, cannot place orders.
CREATE TABLE electronx.traders (
    trader_id          nvarchar(128) NOT NULL PRIMARY KEY,  -- our user id, as on the login page (alice)
    exchange_trader_id varchar(128)  NULL,      -- ElectronX trader id for SenderSubID(50); NULL: OE_DEFAULT_TRADER
    active             bit           NOT NULL DEFAULT 1,
    note               nvarchar(400) NOT NULL DEFAULT N'',
    updated_at         datetime2(3)  NOT NULL DEFAULT sysutcdatetime()
);

-- What each trader may trade: a contract is allowed if any row matches its trader, market and product.
-- Everyone may still see every product; these rows only decide what they may trade.
CREATE TABLE electronx.entitlements (
    trader_id  nvarchar(128) NOT NULL,  -- or '*' for every trader
    market     varchar(16)   NOT NULL,  -- ERCOT, PJM, MISO, CAISO, SPP, ISO-NE, NYISO, or '*'
    product    varchar(16)   NOT NULL,  -- product code with its size, e.g. BFUT500, BFUT100, or '*'
    updated_at datetime2(3)  NOT NULL DEFAULT sysutcdatetime(),
    PRIMARY KEY (trader_id, market, product)
);

-- Limits. NULL means "not set on this row": the next less specific row applies, and a limit no row
-- sets rejects the order. max_open_orders and max_loss are per trader, so only rows for every market
-- and product ('*', '*') set them.
CREATE TABLE electronx.limits (
    trader_id          nvarchar(128) NOT NULL,  -- or '*' for the desk default
    market             varchar(16)   NOT NULL DEFAULT '*',
    product            varchar(16)   NOT NULL DEFAULT '*',
    max_order_qty      int           NULL,  -- lots per order
    max_order_notional decimal(18,2) NULL,  -- quantity x price per order
    max_open_orders    int           NULL,  -- per trader, all contracts
    max_position       int           NULL,  -- lots per contract, counting resting orders
    max_loss           decimal(18,2) NULL,  -- USD per trader: riskmonitor blocks the trader on reaching it
    updated_at         datetime2(3)  NOT NULL DEFAULT sysutcdatetime(),
    PRIMARY KEY (trader_id, market, product)
);
