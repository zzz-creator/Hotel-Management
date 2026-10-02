-- Migration: 007_loyalty_tiers.sql
-- Adds a LoyaltyTiers table that defines the loyalty program tiers and their perks.
-- Tiers are assigned to rooms based on LIFETIME points earned (SUM of positive
-- LoyaltyTransactions deltas), so redeeming points never demotes a guest.
-- Columns:
--   TierName            -> tier display name (PK)
--   MinLifetimePoints   -> lifetime points required to reach this tier
--   PointsMultiplier    -> x-factor applied to normal points accrual for this tier
--   DiscountPercent     -> loyalty discount (%) applied to the pre-tax bill at checkout
--   Perks               -> human-readable description of extra perks for this tier
-- Safe to re-run. Apply from SSMS after migrations 001-006.
USE hotelSystem
GO

IF OBJECT_ID('dbo.LoyaltyTiers', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.LoyaltyTiers (
        TierName NVARCHAR(50) NOT NULL PRIMARY KEY,
        MinLifetimePoints INT NOT NULL DEFAULT (0),
        PointsMultiplier DECIMAL(5,2) NOT NULL DEFAULT (1.00),
        DiscountPercent DECIMAL(5,2) NOT NULL DEFAULT (0),
        Perks NVARCHAR(500) NOT NULL DEFAULT ('')
    );
END
GO

-- Seed default tiers (idempotent: only inserts tiers that are missing).
IF NOT EXISTS (SELECT 1 FROM dbo.LoyaltyTiers WHERE TierName = 'Bronze')
    INSERT INTO dbo.LoyaltyTiers (TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks)
    VALUES ('Bronze', 0, 1.00, 0, N'Standard points accrual. No extra perks.');
IF NOT EXISTS (SELECT 1 FROM dbo.LoyaltyTiers WHERE TierName = 'Silver')
    INSERT INTO dbo.LoyaltyTiers (TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks)
    VALUES ('Silver', 1000, 1.25, 5, N'+25% points on orders; 5% discount on room service; priority concierge.');
IF NOT EXISTS (SELECT 1 FROM dbo.LoyaltyTiers WHERE TierName = 'Gold')
    INSERT INTO dbo.LoyaltyTiers (TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks)
    VALUES ('Gold', 5000, 1.50, 10, N'+50% points on orders; 10% discount on room service; complimentary late check-out; free fitness class.');
IF NOT EXISTS (SELECT 1 FROM dbo.LoyaltyTiers WHERE TierName = 'Platinum')
    INSERT INTO dbo.LoyaltyTiers (TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks)
    VALUES ('Platinum', 20000, 2.00, 15, N'2x points on orders; 15% discount on room service; complimentary breakfast; spa credit; dedicated concierge line.');
GO