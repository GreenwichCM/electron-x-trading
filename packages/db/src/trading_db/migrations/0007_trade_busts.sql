-- Trade busts: the exchange can cancel a trade after the fact (ExecutionReport with ExecType H, and
-- ExecRefID naming the fill). The fill stays in electronx.fills, marked busted; count only the fills
-- WHERE busted_at IS NULL for what stands. Rows recorded before this migration have NULL here.

ALTER TABLE electronx.fills ADD
    busted_at    datetime2(7)  NULL,  -- when the exchange cancelled it (TransactTime of the bust)
    bust_exec_id varchar(64)   NULL,  -- ExecID(17) of the bust report
    bust_reason  nvarchar(400) NULL;  -- Text(58) of the bust report

ALTER TABLE electronx.orders ADD
    busted_qty int NULL;  -- lots of its fills since busted; cum_qty still counts them, as the exchange does

ALTER TABLE electronx.order_events ADD
    busted_qty  int         NULL,
    exec_ref_id varchar(64) NULL;  -- ExecRefID(19): on a trade_bust event, the ExecID of the fill it cancels

ALTER TABLE electronx.exchange_reports ADD
    exec_ref_id varchar(64) NULL;  -- ExecRefID(19)
