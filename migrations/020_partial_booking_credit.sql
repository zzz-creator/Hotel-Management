-- Migration: 020_partial_booking_credit.sql
-- Records exactly how much of each booking payment a check-out invoice consumed.
--
-- The problem:
--   `ReservationPayments.IsApplied` is a BIT and `AppliedToInvoiceID` is a single FK, so
--   a payment row can only be recorded as consumed WHOLE or not at all. But a booking
--   quote is only an estimate -- an admin can cut a room rate between booking and
--   arrival -- so a guest who prepaid $300 can legitimately be billed $250. The old
--   `apply_booking_credit()` marked every unspent row for the stay as applied, so that
--   invoice claimed $300 of credit for a $250 bill and the $50 surplus was recorded as
--   spent. The opposite bug was not reachable (the credit is capped at what is owed), so
--   the fix is to make the surplus representable rather than to choose a side.
--
-- The fix:
--   `AppliedAmount` records the portion of a row that has been given to invoices so far.
--   Outstanding credit becomes SUM(Amount - AppliedAmount) instead of SUM(Amount) over
--   unapplied rows, and `IsApplied` is kept as the cheap "fully consumed" flag that the
--   existing index and the settled-once guarantee rely on. It is still set the moment a
--   row is fully consumed, so nothing that filters on IsApplied changes meaning.
--
--   Credit is consumed OLDEST ROW FIRST, so a partly-used row is the guest's most recent
--   payment and the remainder stays grouped with the money they can still ask about.
--
-- Backfill:
--   Before this migration IsApplied = 1 meant the entire row was consumed, so
--   AppliedAmount = Amount for those rows is exact, not an assumption. Rows left at 0
--   are unconsumed, which is also exact.
--
-- Also renames the Invoices.PrepaidAmount default constraint, added by 018 under the
-- wrong name (DF_Invoices_PaidAmount) -- harmless, but it misleads anyone reading the
-- schema.
--
-- Requires migration 018. Safe to re-run (idempotent).
-- Apply from SSMS after migration 018.
USE hotelSystem
GO

IF COL_LENGTH('dbo.ReservationPayments', 'AppliedAmount') IS NULL
    ALTER TABLE dbo.ReservationPayments
        ADD AppliedAmount DECIMAL(12,2) NOT NULL
            CONSTRAINT DF_ReservationPayments_AppliedAmount DEFAULT (0);
GO

-- Exact backfill: pre-020, a set IsApplied meant the whole row was spent.
UPDATE dbo.ReservationPayments
   SET AppliedAmount = Amount
 WHERE IsApplied = 1 AND AppliedAmount <> Amount;
GO

-- Outstanding credit is now read per stay as SUM(Amount - AppliedAmount) over every row
-- of that stay, so the leading IsApplied column no longer helps the seek. The index is
-- still worth keeping for the per-row consume walk in apply_booking_credit(), which
-- filters the stay and orders by PaymentID.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ReservationPayments_Stay' AND object_id = OBJECT_ID('dbo.ReservationPayments'))
    CREATE INDEX IX_ReservationPayments_Stay ON dbo.ReservationPayments (RoomNumber, StayCheckIn, IsApplied);
GO

-- Fix the mis-named default constraint from 018. The DROP is guarded on the old name so
-- a database where it was never created (or already renamed) is left alone.
IF EXISTS (SELECT 1 FROM sys.default_constraints WHERE name = 'DF_Invoices_PaidAmount')
   AND NOT EXISTS (SELECT 1 FROM sys.default_constraints WHERE name = 'DF_Invoices_PrepaidAmount')
BEGIN
    ALTER TABLE dbo.Invoices DROP CONSTRAINT DF_Invoices_PaidAmount;
    ALTER TABLE dbo.Invoices ADD CONSTRAINT DF_Invoices_PrepaidAmount DEFAULT (0) FOR PrepaidAmount;
END
GO

-- A row can never give out more than it holds, nor more than it took in. This is the
-- invariant that makes SUM(Amount - AppliedAmount) trustworthy, and it is worth a
-- constraint rather than only app-side care: a negative Amount (a refund row) must be
-- able to go to AppliedAmount = 0, never below it, or the sum would count a phantom
-- second refund.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_ReservationPayments_AppliedAmount' AND parent_object_id = OBJECT_ID('dbo.ReservationPayments'))
    ALTER TABLE dbo.ReservationPayments
        ADD CONSTRAINT CK_ReservationPayments_AppliedAmount
        CHECK (AppliedAmount >= 0 AND (Amount <= 0 OR AppliedAmount <= Amount));
GO

-- Reconcile IsApplied with the amount actually consumed, in both directions, so the two
-- columns agree for any row written before this migration. No CHECK is added on that
-- pair deliberately: apply_booking_credit() sets AppliedAmount and then IsApplied as two
-- statements, so a strict consistency CHECK would reject the legitimate intermediate
-- state of "fully consumed, not yet flagged".
UPDATE dbo.ReservationPayments
   SET IsApplied = 1
 WHERE IsApplied = 0 AND AppliedAmount >= Amount;
GO
UPDATE dbo.ReservationPayments
   SET IsApplied = 0
 WHERE IsApplied = 1 AND AppliedAmount < Amount;
GO
