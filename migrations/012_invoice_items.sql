-- Migration: 012_invoice_items.sql
-- Links Transactions line items to the Invoices created at check-out so invoices can
-- be printed with a full itemized breakdown, including items that were paid earlier
-- (pay-now orders) alongside the items settled at check-out.
--
--   Transactions.InvoiceID  : FK -> Invoices.InvoiceID (NULL until an invoice is made)
--   Transactions.PaidEarlier: 1 = this line was billed before check-out (pay-now order),
--                              0 = settled as part of the check-out bill
--
-- bill_room_transactions() sets InvoiceID/PaidEarlier=0 on the items it bills, then
-- links any previously-paid (IsBilled = 1, InvoiceID IS NULL) items with PaidEarlier=1.
--
-- Requires migration 011 (Invoices). Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-011.
USE hotelSystem
GO

IF COL_LENGTH('dbo.Transactions', 'InvoiceID') IS NULL
    ALTER TABLE dbo.Transactions ADD InvoiceID INT NULL;
GO

IF COL_LENGTH('dbo.Transactions', 'PaidEarlier') IS NULL
    ALTER TABLE dbo.Transactions ADD PaidEarlier BIT NOT NULL CONSTRAINT DF_Transactions_PaidEarlier DEFAULT (0);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_Transactions_InvoiceID' AND object_id = OBJECT_ID('dbo.Transactions'))
    CREATE INDEX IX_Transactions_InvoiceID ON dbo.Transactions (InvoiceID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Transactions_Invoices')
    ALTER TABLE dbo.Transactions
        ADD CONSTRAINT FK_Transactions_Invoices FOREIGN KEY (InvoiceID)
        REFERENCES dbo.Invoices (InvoiceID);
GO