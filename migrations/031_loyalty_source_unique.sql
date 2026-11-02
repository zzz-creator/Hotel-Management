-- 031_loyalty_source_unique.sql
-- Phase 2B: unique index on LoyaltyTransactions.SourceID (non-NULL only) so
-- concurrent accrual/redemption attempts cannot double-write a ledger row.
-- Re-runnable.

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID('dbo.LoyaltyTransactions')
                 AND name = 'UX_LoyaltyTransactions_SourceID')
BEGIN
    CREATE UNIQUE INDEX UX_LoyaltyTransactions_SourceID
        ON dbo.LoyaltyTransactions (SourceID)
        WHERE SourceID IS NOT NULL;
END
GO
