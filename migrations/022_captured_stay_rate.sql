-- Migration: 022_captured_stay_rate.sql
-- Locks a stay's nightly rate to the value agreed when it was booked.
--
-- The problem:
--   `post_room_charge()` read `RoomTypes.NightlyRate` at CHECK-OUT time, so the room
--   charge for a stay was priced by whatever the rate card happened to say on the day
--   the guest left. A confirmed booking was therefore re-priced by any admin rate edit
--   made between booking and arrival -- in both directions, silently, with the guest
--   finding out at the till. A rate card is a price LIST; a booking is a contract.
--
--   The guest-facing text said the opposite, and worse: the booking confirmation told the
--   guest "Rates are re-priced at check-out, so the final total may differ", which is an
--   admission that the quoted number is not the number. A quote can be an estimate
--   (F&B is not in it yet); the RATE cannot.
--
-- The fix:
--   `Reservations.NightlyRate` records the per-night rate in force at the moment the stay
--   was made, and `post_room_charge()` bills that number. The rate written onto the folio
--   line and the rate on the reservation are the same value, so the invoice snapshot and
--   the captured rate cannot disagree.
--
--   NULL is meaningful and is preserved by the backfill: it means "this stay predates
--   rate capture", which is what tells check-out to fall back to the current rate and say
--   so out loud, rather than quietly billing a number nobody agreed to. It is never
--   stored as 0, because 0 would be indistinguishable from a genuinely free room.
--
-- Backfill:
--   Existing stays are given their room's CURRENT category rate. That is the best
--   available answer, and it is the same number the pre-022 code would have billed them
--   with today -- so applying this migration does not change any stay that has not yet
--   been billed. A stay whose room has left `Rooms` (or has no configured rate) stays
--   NULL, which is the honest answer: nobody captured a rate for it.
--
-- Related:
--   `ReservationArchive.NightlyRate` already existed, but `_archive_row()` filled it from
--   the live `RoomTypes` rate at archive time. The code now reads the captured rate first,
--   so an archived stay records what it was actually billed rather than what its category
--   costs today.
--
-- Requires migration 013 (RoomTypes) for the backfill; without it the UPDATE matches
-- nothing and every row stays NULL, which check-out handles. Safe to re-run (idempotent).
-- Apply from SSMS after migration 021.
USE hotelSystem
GO

IF COL_LENGTH('dbo.Reservations', 'NightlyRate') IS NULL
    ALTER TABLE dbo.Reservations ADD NightlyRate DECIMAL(10,2) NULL;
GO

-- Backfill from the room's current category rate. LEFT JOIN so a room missing from
-- `Rooms` (or a category missing from `RoomTypes`) simply stays NULL rather than being
-- dropped from the UPDATE; only a rate that is actually positive is stored.
UPDATE r
   SET r.NightlyRate = rt.NightlyRate
  FROM dbo.Reservations r
  JOIN dbo.Rooms rm ON rm.RoomNumber = r.RoomNumber
  JOIN dbo.RoomTypes rt ON rt.RoomType = rm.RoomType
 WHERE r.NightlyRate IS NULL
   AND rt.NightlyRate IS NOT NULL
   AND rt.NightlyRate > 0;
GO

-- Check-out reads the column twice per stay (once for the room charge, once for the
-- archive snapshot), and the write path reads it on every insert. The PK is
-- RoomNumber, so the lookup is already a seek; no index is warranted for four reads.
