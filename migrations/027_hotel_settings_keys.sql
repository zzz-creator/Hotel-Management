-- Migration: 027_hotel_settings_keys.sql
-- Seeds the HotelSettings rows the app now reads directly, so an existing
-- database has the same keys a fresh install gets from database.sql.
--
-- The problem:
--   The settings the app used to read from config.ini ([hotel] name / tax /
--   lockout, [loyalty] enabled / rates) now have HotelSettings rows behind
--   them, with Python-side fallbacks. A database that missed the onboarding
--   settings step would run on the fallbacks forever with no visible row to
--   edit, and the Setup Checklist / admin screen could not show them.
--
-- The fix:
--   Insert the rows where absent, with the same defaults the fallbacks use.
--   Existing rows are left exactly as they are, so admin customisation
--   survives a re-run. No new columns or tables -- data only.
--
-- Safe to re-run: the WHERE NOT EXISTS guard skips rows that exist.
-- Apply from SSMS after migration 026.
USE hotelSystem
GO

INSERT INTO dbo.HotelSettings (SettingKey, SettingValue)
SELECT v.SettingKey, v.SettingValue
FROM (VALUES
    (N'hotel_name', N'The Grand Oasis Hotel'),
    (N'loyalty_enabled', N'1'),
    (N'lockout_threshold', N'3'),
    (N'lockout_duration', N'5'),
    (N'customer_login_max_attempts', N'3')
) AS v(SettingKey, SettingValue)
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.HotelSettings h WHERE h.SettingKey = v.SettingKey
);
GO
