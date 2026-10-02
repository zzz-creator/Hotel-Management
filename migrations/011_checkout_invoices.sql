-- Migration: 011_checkout_invoices.sql
-- Adds a permanent Invoices table recording every settled check-out bill.
-- Each invoice snapshot is created inside bill_room_transactions() when a bill is
-- paid (Transactions marked IsBilled = 1), capturing the pre-discount subtotal,
-- discount-code and tier savings, tax, redemption used, and the amount actually paid.
--
-- No FK is added (room numbers are free-text <floor><3-digit-code>, matching the
-- Reservation/Transactions design).
--
-- Safe to re-run (idempotent). Apply from SSMS after migrations 001-010.
USE hotelSystem
GO

IF OBJECT_ID(N'dbo.Invoices', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Invoices (
        InvoiceID             INT IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_Invoices PRIMARY KEY (InvoiceID),
        RoomNumber            NVARCHAR(50) NOT NULL,
        InvoiceDate           DATETIME NOT NULL DEFAULT GETDATE(),
        Subtotal              DECIMAL(12,2) NOT NULL DEFAULT (0),
        DiscountCodeAmount    DECIMAL(12,2) NOT NULL DEFAULT (0),
        TierDiscountAmount    DECIMAL(12,2) NOT NULL DEFAULT (0),
        TaxAmount             DECIMAL(12,2) NOT NULL DEFAULT (0),
        TotalAmount           DECIMAL(12,2) NOT NULL DEFAULT (0),
        PointsRedeemed        INT NOT NULL DEFAULT (0),
        RedemptionValue       DECIMAL(12,2) NOT NULL DEFAULT (0),
        AmountPaid            DECIMAL(12,2) NOT NULL DEFAULT (0)
    );

    CREATE INDEX IX_Invoices_RoomNumber ON dbo.Invoices (RoomNumber);
END
GO