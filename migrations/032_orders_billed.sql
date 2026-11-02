-- 032_orders_billed.sql
-- Phase 2A: Orders gains Billed/BilledAt for claim-based F&B charge posting.
-- Re-runnable.

IF COL_LENGTH('dbo.Orders', 'Billed') IS NULL
BEGIN
    ALTER TABLE dbo.Orders ADD Billed BIT NOT NULL DEFAULT (0);
END
GO

IF COL_LENGTH('dbo.Orders', 'BilledAt') IS NULL
BEGIN
    ALTER TABLE dbo.Orders ADD BilledAt DATETIME NULL;
END
GO
