-- Starting users and rules for development (`python -m trading_db seed-dev`): the test users on the
-- development login page, all @example.test. Only used when electronx.users is empty, so it never
-- overwrites anything someone has set.

INSERT electronx.users (user_id, display_name, active, note) VALUES
    (N'alice@example.test', N'Alice (trader)',     1, N'test trader: ERCOT only'),
    (N'bob@example.test',   N'Bob (risk)',         1, N'test risk user'),
    (N'carol@example.test', N'Carol (management)', 1, N'test management user'),
    (N'dave@example.test',  N'Dave (trader)',      1, N'test trader: PJM only'),
    (N'erin@example.test',  N'Erin (risk)',        1, N'a second test risk user, to approve bob''s changes');

INSERT electronx.user_roles (user_id, role) VALUES
    (N'alice@example.test', 'trader'),
    (N'bob@example.test',   'risk'),
    (N'carol@example.test', 'management'),
    (N'dave@example.test',  'trader'),
    (N'erin@example.test',  'risk');

INSERT electronx.accounts (account_id, name, capacity) VALUES
    ('TA-HURON-00001', N'test account (the simulator''s)', '2');

INSERT electronx.account_access (user_id, account_id) VALUES
    (N'alice@example.test', 'TA-HURON-00001'),
    (N'dave@example.test',  'TA-HURON-00001');

INSERT electronx.entitlements (trader_id, market, product) VALUES
    (N'alice@example.test', 'ERCOT', '*'),
    (N'dave@example.test',  'PJM',   '*');

INSERT electronx.limits (trader_id, market, product, max_order_qty, max_order_notional, max_open_orders, max_position, max_loss) VALUES
    (N'*',                  '*',     '*',       10,   5000, 20,   50,   5000),  -- the desk default
    (N'alice@example.test', 'ERCOT', 'BFUT500', NULL, NULL, NULL, 20,   NULL);  -- at most 20 lots per ERCOT BFUT500 contract
