-- Migration: 006_remove_inventory.sql
-- Removes the Inventory (housekeeping stock) feature entirely. The application
-- no longer references this table, and the admin menu no longer exposes it.
-- The FK to Items is dropped automatically when the table is dropped.
USE hotelSystem
GO
IF OBJECT_ID('dbo.Inventory', 'U') IS NOT NULL
BEGIN
    DROP TABLE dbo.Inventory;
END
GO