-- Migration: 028_invoice_voids.sql
-- Voiding an invoice keeps the row but stops it counting toward revenue.
--
-- The problem:
--   Once check-out posted an invoice there was no way to correct it at all.
--   The choices were to pretend nothing went wrong or to DELETE the row, and
--   deleting an invoice also leaves the Transactions it grouped orphaned and
--   breaks the audit trail -- the exact columns, amounts and timestamps of the
--   mistake are gone.
--
-- The fix:
--   Three nullable columns on Invoices: VoidedAt, VoidedBy and VoidReason.
--   void_invoice() stamps them; revenue readers filter VoidedAt IS NULL, so a
--   voided invoice drops out of the revenue report and ADR while staying
--   printed in the invoices export for forensics. Anything already voided
--   keeps its snapshot amounts, so history is frozen, not rewritten.
--
-- Safe to re-run: COL_LENGTH guards skip columns that exist.
-- Apply from SSMS after migration 027.
USE hotelSystem
GO

IF COL_LENGTH('dbo.Invoices', 'VoidedAt') IS NULL
    ALTER TABLE dbo.Invoices ADD VoidedAt DATETIME NULL;
GO

IF COL_LENGTH('dbo.Invoices', 'VoidedBy') IS NULL
    ALTER TABLE dbo.Invoices ADD VoidedBy NVARCHAR(50) NULL;
GO

IF COL_LENGTH('dbo.Invoices', 'VoidReason') IS NULL
    ALTER TABLE dbo.Invoices ADD VoidReason NVARCHAR(500) NULL;
GO
