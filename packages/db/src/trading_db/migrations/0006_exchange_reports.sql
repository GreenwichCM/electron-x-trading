-- Every ExecutionReport the drop copy receives, ours and those of orders from the ElectronX screen, as
-- received: the exchange's own record. `python -m trading_db reconcile` compares it with what orderentry's
-- session reported (electronx.order_events), report by report.
CREATE TABLE electronx.exchange_reports (
    exec_id            varchar(64)   NOT NULL PRIMARY KEY,  -- ExecID(17)
    stream_id          varchar(32)   NOT NULL,
    received_at        datetime2(3)  NULL,      -- when the drop copy received it (UTC)
    exchange_time      datetime2(7)  NULL,      -- TransactTime(60)
    order_id           varchar(64)   NOT NULL,  -- OrderID(37)
    cl_ord_id          varchar(64)   NOT NULL,  -- ClOrdID(11)
    orig_cl_ord_id     varchar(64)   NULL,      -- OrigClOrdID(41), on an amend or cancel
    exchange_user      varchar(128)  NULL,      -- TargetSubID(57): the trader's ElectronX id
    account            varchar(64)   NULL,      -- Account(1)
    account_type       varchar(2)    NULL,      -- AccountType(581)
    capacity           varchar(4)    NULL,      -- CustOrderCapacity(582)
    manual             bit           NULL,      -- ManualOrderIndicator(1028)
    symbol             varchar(64)   NOT NULL,
    side               varchar(4)    NOT NULL,
    exec_type          varchar(2)    NOT NULL,  -- ExecType(150)
    ord_status         varchar(2)    NOT NULL,  -- OrdStatus(39)
    order_qty          int           NULL,      -- OrderQty(38)
    price              decimal(18,6) NULL,      -- Price(44)
    tif                varchar(4)    NULL,      -- TimeInForce(59)
    cum_qty            int           NULL,      -- CumQty(14)
    leaves_qty         int           NULL,      -- LeavesQty(151)
    avg_px             decimal(18,6) NULL,      -- AvgPx(6)
    last_qty           int           NULL,      -- LastQty(32), on a fill
    last_px            decimal(18,6) NULL,      -- LastPx(31), on a fill
    trade_id           varchar(64)   NULL,      -- TrdMatchID(880)
    aggressor          bit           NULL,      -- AggressorIndicator(1057)
    commission         decimal(18,6) NULL,      -- Commission(12)
    reject_code        int           NULL,      -- OrdRejReason(103)
    restatement_reason int           NULL,      -- ExecRestatementReason(378)
    text               nvarchar(400) NULL,      -- Text(58)
    raw                nvarchar(max) NOT NULL,  -- the whole message, tag=value|tag=value...
    recorded_at        datetime2(3)  NOT NULL DEFAULT sysutcdatetime()
);
CREATE INDEX ix_exchange_reports_order ON electronx.exchange_reports (order_id, exec_type);
CREATE INDEX ix_exchange_reports_received ON electronx.exchange_reports (received_at);

-- For matching the other way: orderentry's reports by order and type
CREATE INDEX ix_order_events_order_exec ON electronx.order_events (order_id, exec_type);
