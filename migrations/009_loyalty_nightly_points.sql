-- Migration: 009_loyalty_nightly_points.sql
-- Switches loyalty earning to a per-night model and exposes every new parameter
-- through the HotelSettings key/value table so admins can edit them in the app
-- ("Pricing & Settings" and "Loyalty Management" menus).
--
-- New keys:
--   loyalty_points_per_night          -> base points awarded per night stayed
--   loyalty_mult_<room type slug>     -> per-category points multiplier
-- (the existing loyalty_accrual_points_per_unit key now applies to order spend only
--  and its default is lowered from 10 to 3 so room nights are the primary earner).
--
-- Safe to re-run (idempotent). Apply from SSMS after migrations 001-008.
USE hotelSystem
GO

-- Base points per night (default 100).
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_points_per_night')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_points_per_night', '100');
GO

-- Per-room-category point multipliers (higher category -> more points per night).
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_standard')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_standard', '1.0');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_deluxe')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_deluxe', '1.5');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_junior_suite')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_junior_suite', '2.0');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_suite')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_suite', '3.0');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_grand_suite')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_grand_suite', '4.0');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_penthouse')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_penthouse', '6.0');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_mult_presidential_suite')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_mult_presidential_suite', '8.0');
GO

-- Lower the legacy order-spend accrual default only; leave admin-customised values alone.
UPDATE dbo.HotelSettings SET SettingValue = '3'
WHERE SettingKey = 'loyalty_accrual_points_per_unit' AND SettingValue = '10';
GO