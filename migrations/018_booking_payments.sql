-- Migration: 018_booking_payments.sql
-- Public booking desk: guests reserve a room by TYPE from the main menu, pay a
-- deposit or a full prepayment up front, and can cancel for a refund.
--
--   ReservationPayments : the money a guest paid (or was refunded) for a booking.
--                        Rows are SIGNED: charges are positive, refunds and forfeits
--                        are negative, so a stay's net position is a plain SUM().
--   Invoices.PrepaidAmount : the credit actually applied to a check-out invoice.
--
-- Why a separate table instead of `Transactions`:
--   * `Transactions` is the guest FOLIO. Putting a booking deposit there would make
--     it look like hotel revenue: it would inflate `Transactions.Amount`, the
--     `Invoices` subtotal, and therefore ADR/RevPAR and the revenue report -- even
--     though the same money is deducted again at check-out.
--   * Booking money arrives before there is a folio, and survives even if the stay is
--     cancelled and the reservation row is deleted. Money history must outlive the
--     reservation it was for.
--
-- `StayCheckIn` identifies WHICH stay a payment belongs to. Room numbers get reused
-- after a stay completes, so keying on RoomNumber alone would let an old booking's
-- credit leak onto the next guest's check-out bill. Together with `IsApplied`, it also
-- marks a credit as consumed exactly once, so a retried check-out cannot double-credit.
--
-- `BookingRef` is the guest-facing reference they quote at the desk.
--
-- No FK to Reservations: room numbers are free-text `<floor><3-digit-code>` and the
-- existing schema deliberately avoids constraining them (same reasoning as
-- Transactions, Invoices, KeyCards and DoorEvents).
--
-- Requires migrations 001-017. Safe to re-run (idempotent).
-- Apply from SSMS after migrations 001-017.
USE hotelSystem
GO

IF OBJECT_ID('dbo.ReservationPayments', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.ReservationPayments (
        PaymentID         INT IDENTITY(1,1) NOT NULL,
        RoomNumber        NVARCHAR(50) NOT NULL,
        BookingRef        VARCHAR(20)  NOT NULL,
        StayCheckIn       DATE         NOT NULL,
        Kind              VARCHAR(10)  NOT NULL,
        Amount            DECIMAL(12,2) NOT NULL,
        NightsCovered     INT          NULL,
        CardLast4         VARCHAR(4)   NULL,
        PaidAt            DATETIME     NOT NULL CONSTRAINT DF_ReservationPayments_PaidAt DEFAULT (GETDATE()),
        AppliedToInvoiceID INT         NULL,
        IsApplied         BIT          NOT NULL CONSTRAINT DF_ReservationPayments_IsApplied DEFAULT (0),
        Notes             NVARCHAR(200) NULL,
        CONSTRAINT PK_ReservationPayments PRIMARY KEY CLUSTERED (PaymentID)
    );
END
GO

-- Kind is constrained so a typo cannot invent a money movement the UI cannot render.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_ReservationPayments_Kind' AND parent_object_id = OBJECT_ID('dbo.ReservationPayments'))
    ALTER TABLE dbo.ReservationPayments
        ADD CONSTRAINT CK_ReservationPayments_Kind
        CHECK (Kind IN ('Deposit', 'Prepayment', 'Refund', 'Forfeit'));
GO

-- Outstanding credit is read per stay at check-out: WHERE RoomNumber + StayCheckIn
-- + IsApplied = 0, so this index covers that lookup.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ReservationPayments_Stay' AND object_id = OBJECT_ID('dbo.ReservationPayments'))
    CREATE INDEX IX_ReservationPayments_Stay ON dbo.ReservationPayments (RoomNumber, StayCheckIn, IsApplied);
GO

-- The guest quotes BookingRef at the desk.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ReservationPayments_BookingRef' AND object_id = OBJECT_ID('dbo.ReservationPayments'))
    CREATE INDEX IX_ReservationPayments_BookingRef ON dbo.ReservationPayments (BookingRef);
GO

/* ==================== Invoices: credit applied at check-out ==================== */
IF COL_LENGTH('dbo.Invoices', 'PrepaidAmount') IS NULL
    ALTER TABLE dbo.Invoices
        ADD PrepaidAmount DECIMAL(12,2) NOT NULL CONSTRAINT DF_Invoices_PaidAmount DEFAULT (0);
GO

/* ============================ New setting ==================================== */
-- Free cancellation up to this many days before check-in; later than that the
-- deposit is forfeited. Read via _setting_int('booking_refund_cutoff_days', 7) so a
-- typo in HotelSettings falls back to the default instead of breaking cancellation.
IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = 'booking_refund_cutoff_days')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue) VALUES ('booking_refund_cutoff_days', '7');
GO
