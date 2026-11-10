-- Every change to the trading rules made from the app: proposed by one person, approved or rejected by
-- another, with the rule as it was before and after. Rows are never deleted: this is the audit trail.
-- (Edits made directly in SSMS bypass it; in production, only the app's login should be able to write.)
CREATE TABLE electronx.rule_changes (
    change_id     int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    kind          varchar(16)   NOT NULL,  -- trader, entitlement, limit
    action        varchar(8)    NOT NULL,  -- set (add or change), delete
    trader_id     nvarchar(128) NOT NULL,
    market        varchar(16)   NOT NULL,
    product       varchar(16)   NOT NULL,
    before_json   nvarchar(max) NULL,      -- the rule when proposed; NULL: it did not exist
    after_json    nvarchar(max) NULL,      -- the rule once approved; NULL: removed
    reason        nvarchar(400) NOT NULL,
    status        varchar(16)   NOT NULL DEFAULT 'pending',  -- pending, approved, rejected, withdrawn, stale
    requested_by  nvarchar(128) NOT NULL,
    requested_at  datetime2(3)  NOT NULL DEFAULT sysutcdatetime(),
    decided_by    nvarchar(128) NULL,
    decided_at    datetime2(3)  NULL,
    decision_note nvarchar(400) NOT NULL DEFAULT N''
);
CREATE INDEX ix_rule_changes_status ON electronx.rule_changes (status, requested_at);
