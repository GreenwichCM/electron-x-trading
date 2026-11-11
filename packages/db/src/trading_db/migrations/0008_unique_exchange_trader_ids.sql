-- Each ElectronX trader id belongs to one trader at most, so every order from the ElectronX screen can be
-- given to its trader. The index ignores case like the rest of the database (Jane@x.com = jane@x.com).
IF EXISTS (SELECT 1 FROM electronx.traders WHERE exchange_trader_id IS NOT NULL
           GROUP BY exchange_trader_id HAVING COUNT(*) > 1)
    THROW 50000, 'Two or more traders share an exchange_trader_id. Find them with: SELECT exchange_trader_id, COUNT(*) FROM electronx.traders GROUP BY exchange_trader_id HAVING COUNT(*) > 1; give each ElectronX id to one trader, then run migrate again.', 1;
GO
CREATE UNIQUE INDEX ux_traders_exchange_trader_id ON electronx.traders (exchange_trader_id)
    WHERE exchange_trader_id IS NOT NULL;
