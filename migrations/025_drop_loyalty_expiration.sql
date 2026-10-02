-- Migration: 025_drop_loyalty_expiration.sql
-- Removes a settings key that has never controlled anything.
--
-- The problem:
--   `loyalty_expiration_days` was seeded at 0 with the comment "0 = never expire" and
--   was editable from the admin "Pricing & Settings" screen. It was read by nothing.
--   Not once, anywhere in the codebase. So the screen offered an operator a control
--   labelled "Loyalty Point Expiry (days)" over a programme that does not expire points,
--   and setting it to 90 would have been accepted, stored, displayed, and changed no
--   behaviour whatsoever.
--
--   That is worse than a missing feature. A knob that does nothing is not obviously inert
--   -- it looks exactly like a knob that works, and its only failure mode is invisible.
--   An operator who set 90 and saw it persist would reasonably believe points were being
--   cleared, and would not go looking for a redemption sweep that was never written. The
--   setting and the (non-existent) feature share one lie, which is why removing the
--   setting is the honest move: the app now has no way to promise point expiry, and so
--   makes no such promise.
--
-- The fix:
--   Delete the row, and the code that read and wrote it. Points do not expire. Redemption
--   is driven by a guest's current balance, which is already correct for a programme
--   that rewards recency through tier promotion rather than through a time limit.
--
--   Deleting is safe and is not a data-loss risk: the value is a duplicate of the
--   `never expire` default it was seeded with, and no historical column references it.
--   A fresh install no longer seeds it (see database.sql), so this migration is a no-op
--   there and only does work against a database that has been running since before it.
--
-- Safe to re-run (idempotent): the delete is guarded, so a second run matches no rows.
-- Apply from SSMS after migration 024.
USE hotelSystem
GO

DELETE FROM dbo.HotelSettings WHERE SettingKey = N'loyalty_expiration_days';
GO
