-- For a DBA: give the trading platform its own schema in an existing (possibly shared) database.
-- Run once as a login with rights to create logins and users. Replace <database> and <password>.
-- The platform's login can then create and change only objects in the `trading` schema, and read
-- or write only data in it; the rest of the database stays out of its reach.

USE master;
CREATE LOGIN trading_app WITH PASSWORD = '<password>', CHECK_POLICY = ON;
GO

USE <database>;
CREATE USER trading_app FOR LOGIN trading_app WITH DEFAULT_SCHEMA = trading;
GO
CREATE SCHEMA trading AUTHORIZATION dbo;
GO

-- Read and write the platform's data
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::trading TO trading_app;

-- Run `python -m trading_db migrate`: create and change tables and views in the trading schema.
-- (CREATE TABLE and CREATE VIEW are database-wide permissions, but they only take effect in a
-- schema the login may ALTER, which is just this one.) Revoke between releases if you prefer.
GRANT CREATE TABLE, CREATE VIEW TO trading_app;
GRANT ALTER, REFERENCES ON SCHEMA::trading TO trading_app;
GO
