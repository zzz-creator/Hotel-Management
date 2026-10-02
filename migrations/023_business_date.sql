-- Migration: 023_business_date.sql
-- Gives the hotel one clock for "which day is it", stored in the database.
--
-- The problem:
--   Thirty-odd call sites asked the wall clock. `datetime.now().date()` in Python and
--   `CAST(GETDATE() AS date)` in SQL, and the SQL Server clock is the DATABASE SERVER's,
--   not the desk terminal's -- so on any machine whose time was off, the housekeeping
--   board and the occupancy report were answering about different days from each other.
--
--   Worse, none of them could be re-run. The occupancy report derived its calendar from
--   the stays on record, so it was retrospective; the housekeeping report and the floor
--   explorer read GETDATE(), so they showed the wall clock's today and nothing else.
--   A PMS report you cannot re-run for last Tuesday is not a report. Ask "what did the
--   board say on Tuesday" after Thursday and there was no answer available, only a new
--   number for Thursday.
--
--   The two reports also disagreed with each other about the same rows: the occupancy
--   report counted a night as occupied while `CheckInDate <= Night AND CheckOutDate >
--   Night`, and the housekeeping report used `CheckOutDate >= GETDATE()`, so a guest
--   departing on the 4th appeared in house on the night of the 4th in one report and
--   not the other. Nothing anchored either of them, so the disagreement had no single
--   thing to be a disagreement ABOUT. The code fix is in the same commit as this
--   migration -- both reports now use the half-open window and read the value seeded here.
--
-- The fix:
--   One row, `business_date`, holding an ISO date. `business_date()` in maincopycopy.py
--   and reports.py both read it, and every "today" in the app comes from there. It is
--   advanced one day at a time by `close_day()` from the admin "Business Date" menu, and
--   can be set to an explicit date, which is what makes a past board reproducible.
--
--   This is NOT a night audit, and it is important not to describe it as one. It does
--   not bill, expire, roll, clean, or re-rate anything; it moves one value. A real night
--   audit would post the day's folios and roll occupancy forward as a stored fact, and
--   building that here would have meant a business-objects layer this app does not have.
--
-- Seeding:
--   The row is seeded to the date the migration is applied, which is the day the operator
--   considers the hotel to be on. Before this migration, "today" was the wall clock, so
--   seeding to CAST(GETDATE() AS date) preserves the meaning of every date already on
--   record. An operator who is catching up on a backlog moves it by hand afterwards.
--
--   `business_date()` falls back to the wall clock when this row is missing, blank, or
--   unparseable -- a missing clock must not stop the front desk checking a guest out --
--   but it logs at ERROR level when it does, because the fallback is the difference
--   between a reproducible report and a wrong one.
--
-- Safe to re-run (idempotent): the insert is guarded, so a second run leaves the operator's
-- current business date alone rather than resetting it to the day the script was run.
-- Apply from SSMS after migration 022.
USE hotelSystem
GO

IF NOT EXISTS (SELECT 1 FROM dbo.HotelSettings WHERE SettingKey = N'business_date')
    INSERT INTO dbo.HotelSettings (SettingKey, SettingValue)
    VALUES (N'business_date', CONVERT(NVARCHAR(10), CAST(GETDATE() AS DATE), 23));
GO
