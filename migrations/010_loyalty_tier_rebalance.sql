-- Migration: 010_loyalty_tier_rebalance.sql
-- Recalibrates the two upper loyalty tiers for the per-night earning model.
-- Silver stays at 1,000 lifetime points; Gold 5,000 -> 2,500 and
-- Platinum 20,000 -> 7,500 so the ladder matches real hotel-program pacing
-- (roughly 10 / 22 / 56 standard-room nights).
--
-- Guarded so admin-customised thresholds are not overwritten: each UPDATE only
-- fires while the tier is still at its old default. Run "Recalculate All Tiers"
-- in the admin Loyalty menu afterwards to promote existing accounts.
--
-- Safe to re-run (idempotent). Apply from SSMS after migrations 001-009.
USE hotelSystem
GO

UPDATE dbo.LoyaltyTiers SET MinLifetimePoints = 2500
WHERE TierName = 'Gold' AND MinLifetimePoints = 5000;
GO

UPDATE dbo.LoyaltyTiers SET MinLifetimePoints = 7500
WHERE TierName = 'Platinum' AND MinLifetimePoints = 20000;
GO