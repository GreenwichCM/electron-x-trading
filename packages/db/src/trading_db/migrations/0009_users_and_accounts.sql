-- One identity per person, the way ElectronX knows them: their ElectronX login, which is their company
-- email in lower case. It is the user id everywhere: the sign-in, the trading rules, the orders, and the
-- SenderSubID(50) on every order we send for them; ElectronX's reports name them by it (TargetSubID 57).
-- electronx.users and user_roles replace electronx.traders (and its exchange_trader_id) and the test users
-- that were written into the api; electronx.accounts and account_access replace OE_ACCOUNT/OE_CAPACITY.

CREATE TABLE electronx.users (
    user_id      nvarchar(128) NOT NULL PRIMARY KEY,   -- jane@greenwichcm.com
    display_name nvarchar(128) NOT NULL,
    active       bit           NOT NULL DEFAULT 1,     -- 0: cannot sign in or trade
    note         nvarchar(400) NOT NULL DEFAULT N'',
    google_sub   varchar(64)   NULL,                   -- their Google account, once they have signed in with it
    updated_at   datetime2(3)  NOT NULL DEFAULT sysutcdatetime(),
    CONSTRAINT ck_users_email CHECK (user_id LIKE N'%_@_%.%'),
    CONSTRAINT ck_users_lower_case CHECK (user_id COLLATE Latin1_General_100_BIN2 = LOWER(user_id))
);
CREATE UNIQUE INDEX ux_users_google_sub ON electronx.users (google_sub) WHERE google_sub IS NOT NULL;

-- What each person does here: trader (may place orders), risk, management. See ROLE_PERMISSIONS in the api.
CREATE TABLE electronx.user_roles (
    user_id nvarchar(128) NOT NULL REFERENCES electronx.users ON DELETE CASCADE,
    role    varchar(20)   NOT NULL CONSTRAINT ck_user_roles_role CHECK (role IN ('trader', 'risk', 'management')),
    PRIMARY KEY (user_id, role)
);

-- The ElectronX trading accounts (Account, tag 1), and who may trade on each.
CREATE TABLE electronx.accounts (
    account_id varchar(64)   NOT NULL PRIMARY KEY,  -- TA-HURON-00001
    name       nvarchar(128) NOT NULL DEFAULT N'',
    capacity   varchar(4)    NOT NULL DEFAULT '2',  -- CustOrderCapacity(582) sent with every order on it
    active     bit           NOT NULL DEFAULT 1,
    updated_at datetime2(3)  NOT NULL DEFAULT sysutcdatetime()
);
CREATE TABLE electronx.account_access (
    user_id    nvarchar(128) NOT NULL REFERENCES electronx.users ON DELETE CASCADE,
    account_id varchar(64)   NOT NULL REFERENCES electronx.accounts,
    PRIMARY KEY (user_id, account_id)
);
GO

-- Everyone who had a user id becomes a user: a trader with an ElectronX login keeps it as their id; one
-- without (the test traders) becomes <id>@example.test, like the other test users.
CREATE TABLE #ids (
    old_id nvarchar(128) NOT NULL PRIMARY KEY,
    new_id nvarchar(128) NOT NULL,
    name   nvarchar(128) NOT NULL,
    active bit           NOT NULL,
    note   nvarchar(400) NOT NULL,
    role   varchar(20)   NOT NULL
);
INSERT #ids (old_id, new_id, name, active, note, role)
SELECT t.trader_id,
       LOWER(CASE WHEN t.exchange_trader_id LIKE N'%_@_%.%' THEN t.exchange_trader_id
                  ELSE t.trader_id + N'@example.test' END),
       CASE WHEN t.exchange_trader_id LIKE N'%_@_%.%'
                THEN LEFT(t.exchange_trader_id, CHARINDEX(N'@', t.exchange_trader_id) - 1)
            WHEN t.trader_id IN (N'alice', N'dave')
                THEN UPPER(LEFT(t.trader_id, 1)) + SUBSTRING(t.trader_id, 2, 127) + N' (trader)'
            ELSE t.trader_id END,
       t.active, t.note, 'trader'
FROM electronx.traders AS t;

-- In a database seeded for development, the test users who are not traders as well.
IF EXISTS (SELECT 1 FROM #ids WHERE old_id IN (N'alice', N'dave'))
    INSERT #ids (old_id, new_id, name, active, note, role)
    SELECT v.old_id, v.old_id + N'@example.test', v.name, 1, v.note, v.role
    FROM (VALUES (N'bob', N'Bob (risk)', N'test risk user', 'risk'),
                 (N'carol', N'Carol (management)', N'test management user', 'management'),
                 (N'erin', N'Erin (risk)', N'a second test risk user, to approve bob''s changes', 'risk'))
         AS v(old_id, name, note, role)
    WHERE NOT EXISTS (SELECT 1 FROM #ids WHERE old_id = v.old_id);

INSERT electronx.users (user_id, display_name, active, note)
SELECT new_id, MIN(name), MAX(CAST(active AS int)), MIN(note) FROM #ids GROUP BY new_id;
INSERT electronx.user_roles (user_id, role) SELECT DISTINCT new_id, role FROM #ids;

-- The accounts orders have gone out on, and in a development database the simulator's; every trader
-- may trade on them.
INSERT electronx.accounts (account_id)
SELECT DISTINCT a FROM (SELECT account AS a FROM electronx.orders UNION SELECT account FROM electronx.fills) AS x
WHERE a IS NOT NULL AND a <> '';
IF NOT EXISTS (SELECT 1 FROM electronx.accounts) AND EXISTS (SELECT 1 FROM #ids WHERE new_id LIKE N'%@example.test')
    INSERT electronx.accounts (account_id, name) VALUES ('TA-HURON-00001', N'test account (the simulator''s)');
INSERT electronx.account_access (user_id, account_id)
SELECT DISTINCT i.new_id, a.account_id FROM #ids AS i CROSS JOIN electronx.accounts AS a WHERE i.role = 'trader';

-- Every old id in the rules and the history becomes the new one.
UPDATE x SET trader_id = i.new_id FROM electronx.entitlements AS x JOIN #ids AS i ON i.old_id = x.trader_id;
UPDATE x SET trader_id = i.new_id FROM electronx.limits AS x JOIN #ids AS i ON i.old_id = x.trader_id;
UPDATE x SET trader = i.new_id FROM electronx.orders AS x JOIN #ids AS i ON i.old_id = x.trader;
UPDATE x SET trader = i.new_id FROM electronx.order_events AS x JOIN #ids AS i ON i.old_id = x.trader;
UPDATE x SET by_user = i.new_id FROM electronx.order_events AS x JOIN #ids AS i ON i.old_id = x.by_user;
UPDATE x SET trader_id = i.new_id FROM electronx.rule_changes AS x JOIN #ids AS i ON i.old_id = x.trader_id;
UPDATE x SET requested_by = i.new_id FROM electronx.rule_changes AS x JOIN #ids AS i ON i.old_id = x.requested_by;
UPDATE x SET decided_by = i.new_id FROM electronx.rule_changes AS x JOIN #ids AS i ON i.old_id = x.decided_by;
UPDATE x SET trader = i.new_id FROM electronx.alerts AS x JOIN #ids AS i ON i.old_id = x.trader;
UPDATE x SET by_user = i.new_id FROM electronx.alerts AS x JOIN #ids AS i ON i.old_id = x.by_user;

-- A trader change still waiting for approval no longer fits: propose it again as a user change.
UPDATE electronx.rule_changes
SET status = 'stale', decided_at = sysutcdatetime(),
    decision_note = N'traders became users (migration 0009): propose it again'
WHERE kind = 'trader' AND status = 'pending';

DROP TABLE #ids;
DROP TABLE electronx.traders;
