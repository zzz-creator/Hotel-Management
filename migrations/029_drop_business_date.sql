-- Migration: 029_drop_business_date.sql
-- Deletes the legacy HotelSettings['business_date'] row for good.
--
-- The problem:
--   The row has been ignored since 5 October 2026 (business_date() is the wall
--   clock), but it was still seeded by database.sql and sat in every HotelSettings
--   screen as a knob that lies. Migration 025 deleted the equally-dead
--   loyalty_expiration_days row; business_date never got the same follow-through.
--
-- The fix:
--   One guarded DELETE. Idempotent; safe on a database that never had the row.
--   HotelSettings keeps every other row untouched because the delete is keyed on
--   this one SettingKey.
--
-- Apply from SSMS after migration 028.
USE hotelSystem
GO

IF EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = N'business_date')
    DELETE FROM dbo.HotelSettings WHERE SettingKey = N'business_date';
GO
