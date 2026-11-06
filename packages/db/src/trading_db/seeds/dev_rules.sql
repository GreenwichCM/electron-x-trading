-- Starting rules for the development test users (`python -m trading_db seed-dev`).
-- Only used when electronx.traders is empty, so it never overwrites rules someone has set.

INSERT electronx.traders (trader_id, exchange_trader_id, active, note) VALUES
    (N'alice', NULL, 1, N'test trader: ERCOT only'),
    (N'dave',  NULL, 1, N'test trader: PJM only');

INSERT electronx.entitlements (trader_id, market, product) VALUES
    (N'alice', 'ERCOT', '*'),
    (N'dave',  'PJM',   '*');

INSERT electronx.limits (trader_id, market, product, max_order_qty, max_order_notional, max_open_orders, max_position, max_loss) VALUES
    (N'*',     '*',     '*',       10,   5000, 20,   50,   5000),  -- the desk default
    (N'alice', 'ERCOT', 'BFUT500', NULL, NULL, NULL, 20,   NULL);  -- alice: at most 20 lots per ERCOT BFUT500 contract
