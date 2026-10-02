-- Migration: 005_pricing_settings.sql
-- Makes hotel business rules editable from the admin panel ("Pricing & Settings").
-- Adds a key/value HotelSettings table that stores:
--   peak_factor / offpeak_factor  -> price multipliers applied to item base price
--   tax_rate                      -> sales tax as a decimal (0.13 = 13%)
--   loyalty_*                     -> loyalty accrual/redemption/expiry rates
-- Values are NVARCHAR so they can be edited freely; Python parses them on read.
-- SQL values below mirror the defaults currently in config.ini.
USE hotelSystem
GO

IF OBJECT_ID('dbo.HotelSettings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.HotelSettings (
        SettingKey NVARCHAR(50) NOT NULL PRIMARY KEY,
        SettingValue NVARCHAR(100) NULL
    );
END
GO

-- Peak / off-peak price multipliers (applied in get_dynamic_price()).
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'peak_factor')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('peak_factor', '1.20');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'offpeak_factor')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('offpeak_factor', '0.90');
GO

-- Tax rate as a decimal (0.13 == 13%).
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'tax_rate')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('tax_rate', '0.13');
GO

-- Loyalty rates.
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_accrual_points_per_unit')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_accrual_points_per_unit', '10');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_redemption_points_per_currency_unit')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_redemption_points_per_currency_unit', '100');
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'loyalty_expiration_days')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('loyalty_expiration_days', '0');
GO