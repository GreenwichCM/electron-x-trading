-- Everything an order event carries: what happened, who or what caused it, and the exchange's report.
-- Rows recorded before this migration have NULL here.

ALTER TABLE electronx.order_events ADD
    event              varchar(24)   NULL,  -- new_order, refused, cancel_request, cancel_refused,
                                            -- exchange_report, cancel_reject, business_reject
    by_user            nvarchar(128) NULL,  -- who or what caused it: a user id, an ElectronX user (orders from the
                                            -- screen), the kill switch or block's author, orderentry, exchange
    reason             nvarchar(400) NULL,  -- why: a refusal or reject, the kill switch's reason, the exchange's text
    event_at           datetime2(3)  NULL,  -- when it happened here (UTC)
    request_id         varchar(64)   NULL,  -- the ClOrdID of the cancel (or amend) request it belongs to
    exec_id            varchar(64)   NULL,  -- ExecID(17) of the exchange's report
    exec_type          varchar(2)    NULL,  -- ExecType(150): 0 new, 4 cancelled, 5 replaced, 8 rejected, C expired, F trade
    exchange_time      datetime2(7)  NULL,  -- TransactTime(60): when it happened at the exchange (UTC)
    last_qty           int           NULL,  -- LastQty(32), on a fill
    last_px            decimal(18,6) NULL,  -- LastPx(31), on a fill
    leaves_qty         int           NULL,  -- quantity still working
    reject_code        int           NULL,  -- OrdRejReason(103), CxlRejReason(102) or BusinessRejectReason(380)
    restatement_reason int           NULL,  -- ExecRestatementReason(378): 8 cancel on disconnect, 99 self-match
    trade_id           varchar(64)   NULL,  -- TrdMatchID(880), on a fill
    aggressor          bit           NULL,  -- AggressorIndicator(1057), on a fill
    commission         decimal(18,6) NULL,  -- Commission(12), on a fill
    account_type       varchar(2)    NULL,  -- AccountType(581): 1 customer, 2 non-customer
    account            varchar(64)   NULL,  -- Account(1)
    capacity           varchar(4)    NULL,  -- CustOrderCapacity(582)
    ord_type           varchar(8)    NULL,  -- LIMIT
    manual             bit           NULL;  -- ManualOrderIndicator(1028)

ALTER TABLE electronx.orders ADD
    account    varchar(64) NULL,
    capacity   varchar(4)  NULL,
    ord_type   varchar(8)  NULL,
    manual     bit         NULL,
    leaves_qty int         NULL,
    last_event varchar(24) NULL;

ALTER TABLE electronx.fills ADD
    trade_id   varchar(64)   NULL,  -- TrdMatchID(880)
    aggressor  bit           NULL,  -- AggressorIndicator(1057)
    commission decimal(18,6) NULL,  -- Commission(12)
    leaves_qty int           NULL,  -- LeavesQty(151) after this fill
    capacity   varchar(4)    NULL;  -- CustOrderCapacity(582)

CREATE INDEX ix_order_events_exec ON electronx.order_events (exec_id);
