-- Orders entered on the ElectronX screen, not sent from this app. The drop copy reports them too, so
-- the recorder keeps them in electronx.orders and electronx.order_events alongside ours:
--   source         'app' for orders sent by orderentry, 'electronx' for orders from the ElectronX screen
--   exchange_user  the trader's ElectronX id, TargetSubID(57) on the exchange's reports; for an order
--                  from the screen, `trader` is whoever has it in electronx.traders.exchange_trader_id
--                  (empty if nobody, or more than one trader, has it)
ALTER TABLE electronx.orders ADD
    source        varchar(10)  NOT NULL CONSTRAINT df_orders_source DEFAULT 'app',
    exchange_user varchar(128) NULL;

ALTER TABLE electronx.order_events ADD
    source        varchar(10)  NOT NULL CONSTRAINT df_order_events_source DEFAULT 'app',
    exchange_user varchar(128) NULL;

-- Every fill, ours or from the screen, with the trader's ElectronX id and ManualOrderIndicator(1028).
ALTER TABLE electronx.fills ADD
    exchange_user varchar(128) NULL,
    manual        bit          NULL;
