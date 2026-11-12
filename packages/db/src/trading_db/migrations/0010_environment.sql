-- Which environment this database belongs to: development, beta (ElectronX beta) or production. The first
-- service to connect after this migration writes it (TRADING_ENV); services of another environment then
-- refuse the database, so beta can never write into production's, or production into a test database.
-- Production only marks a database with no orders, fills, reports or users yet. See trading_core.environment.

CREATE TABLE electronx.environment (
    id        tinyint      NOT NULL PRIMARY KEY CONSTRAINT ck_environment_one_row CHECK (id = 1),
    name      varchar(16)  NOT NULL CONSTRAINT ck_environment_name
                               CHECK (name IN ('development', 'beta', 'production')),
    marked_at datetime2(3) NOT NULL DEFAULT sysutcdatetime()
);
