-- 030_concurrency_claims.sql
-- Phase 1 concurrency claims (see plans/PLAN-concurrency.md).
-- 1. Items.ItemID becomes IDENTITY(1,1) (no FKs reference it).
-- 2. Reservations gains RoomChargePosted (claim for post_room_charge).
-- 3. Reservations gains SettlementStatus (tracks check-out step progress).
-- Re-runnable: each block is guarded.

-- 1. Items.ItemID -> IDENTITY(1,1)
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Items') AND name = 'ItemID' AND is_identity = 1)
BEGIN
    CREATE TABLE dbo.Items_new (
        ItemID INT IDENTITY(1,1) NOT NULL,
        Name NVARCHAR(100) NULL,
        Price DECIMAL(10,2) NULL,
        PricingRule NVARCHAR(50) NULL,
        CONSTRAINT PK_Items_new PRIMARY KEY CLUSTERED (ItemID)
    );
    SET IDENTITY_INSERT dbo.Items_new ON;
    INSERT INTO dbo.Items_new (ItemID, Name, Price, PricingRule)
        SELECT ItemID, Name, Price, PricingRule FROM dbo.Items;
    SET IDENTITY_INSERT dbo.Items_new OFF;
    DROP TABLE dbo.Items;
    EXEC sp_rename 'dbo.Items_new', 'Items';
    EXEC sp_rename 'PK_Items_new', 'PK_Items', 'OBJECT';
END
GO

-- 2. Reservations.RoomChargePosted
IF COL_LENGTH('dbo.Reservations', 'RoomChargePosted') IS NULL
BEGIN
    ALTER TABLE dbo.Reservations ADD RoomChargePosted BIT NOT NULL DEFAULT (0);
END
GO

-- 3. Reservations.SettlementStatus
IF COL_LENGTH('dbo.Reservations', 'SettlementStatus') IS NULL
BEGIN
    ALTER TABLE dbo.Reservations ADD SettlementStatus NVARCHAR(20) NULL;
END
GO
