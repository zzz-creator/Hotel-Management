-- 037_night_audit.sql
-- Night-audit snapshot: one row per business date (the wall-clock day).
-- See docs/DEVIATIONS.md section 3: the business date is the wall clock by design;
-- this table is the missing business-objects layer that records that a day was
-- audited/closed, so future accrual reports can anchor to a real audit instead of
-- to "today". No report or scheduler reads it yet -- the table exists first.
IF OBJECT_ID('dbo.NightAudit', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.NightAudit (
        AuditDate    date           NOT NULL PRIMARY KEY,
        CompletedAt  datetime2      NOT NULL DEFAULT (SYSUTCDATETIME()),
        CompletedBy  nvarchar(50)   NULL
    );
END
GO