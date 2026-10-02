-- Migration: 003_reservations.sql
-- Adds sensible defaults and a date-range check for existing Reservations tables
-- so inserts that omit CheckInDate/CheckOutDate still succeed.
USE hotelSystem

IF NOT EXISTS (SELECT 1 FROM sys.default_constraints WHERE parent_object_id = OBJECT_ID('dbo.Reservations') AND name = 'DF_Reservations_CheckInDate')
BEGIN
    ALTER TABLE dbo.Reservations ADD CONSTRAINT DF_Reservations_CheckInDate DEFAULT (CAST(GETDATE() AS DATE)) FOR CheckInDate;
END;

IF NOT EXISTS (SELECT 1 FROM sys.default_constraints WHERE parent_object_id = OBJECT_ID('dbo.Reservations') AND name = 'DF_Reservations_CheckOutDate')
BEGIN
    ALTER TABLE dbo.Reservations ADD CONSTRAINT DF_Reservations_CheckOutDate DEFAULT (CAST(DATEADD(DAY, 1, GETDATE()) AS DATE)) FOR CheckOutDate;
END;

IF OBJECT_ID('dbo.CH_Reservations_DateRange') IS NULL
BEGIN
    ALTER TABLE dbo.Reservations ADD CONSTRAINT CH_Reservations_DateRange CHECK (CheckOutDate > CheckInDate);
END;