-- Migration: 021_booking_ref_uniqueness.sql
-- Makes a booking reference unique by DATABASE guarantee instead of by a probe.
--
-- The problem:
--   `new_booking_ref()` generated a random 6-hex-digit reference and asked
--   `booking_reference_exists()` whether it was taken. That probe ran on its OWN
--   connection, before the booking transaction opened, and nothing in the schema stopped
--   two sessions from both seeing the same reference as free and both taking it. Two
--   guests would then share a reference, and since the reference is the only handle a
--   guest has on their own booking, one of them could view or cancel the other's stay.
--
--   A plain UNIQUE index on BookingRef is not possible either: a cancellation writes a
--   second row carrying the SAME reference (a negative Refund or Forfeit), so one
--   booking legitimately owns several rows.
--
-- The fix:
--   A filtered UNIQUE index over the two kinds that only ever appear ONCE per booking.
--   `book_room()` writes exactly one initial charge (Deposit OR Prepayment) per booking;
--   every later row is a Refund or Forfeit. So "one Deposit/Prepayment row per BookingRef"
--   is exactly "one booking per BookingRef", and the index enforces it.
--
-- Why the filter must be on Kind, not on the whole table:
--   A booking's uniqueness has to survive its reservation being deleted.
--   `cancel_booking()` DELETEs the Reservations row but deliberately leaves the signed
--   payment rows behind -- money history must outlive the reservation. If uniqueness were
--   keyed on Reservations instead, a cancelled reference would become free again and
--   `new_booking_ref()` could reissue it, so a guest holding an old cancelled reference
--   would one day be looking at somebody else's booking. Keying the guarantee on the
--   durable payment rows means a reference is never recycled.
--
-- Requires migration 018. Safe to re-run (idempotent).
-- Apply from SSMS after migration 018.
USE hotelSystem
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_ReservationPayments_BookingRef_Charge' AND object_id = OBJECT_ID('dbo.ReservationPayments'))
BEGIN
    -- Guard against pre-existing duplicates rather than failing the migration: if two
    -- bookings somehow already share a reference, the index cannot be created, and
    -- silently skipping it would hide that. Raise so the operator can look.
    IF EXISTS (
        SELECT 1 FROM dbo.ReservationPayments
        WHERE Kind IN ('Deposit', 'Prepayment')
        GROUP BY BookingRef HAVING COUNT(*) > 1
    )
        RAISERROR('Duplicate booking references exist among Deposit/Prepayment rows; resolve them before applying migration 021.', 16, 1);
    ELSE
        CREATE UNIQUE INDEX UX_ReservationPayments_BookingRef_Charge
            ON dbo.ReservationPayments (BookingRef)
            WHERE Kind IN ('Deposit', 'Prepayment');
END
GO
