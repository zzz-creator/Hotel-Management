-- Migration: 024_loyalty_order_accrual.sql
-- Recalibrates the F&B earn rate so the room is worth more than a drink.
--
-- The problem:
--   The programme has two earn rates. The room pays `loyalty_points_per_night` x category
--   multiplier; room service pays `loyalty_accrual_points_per_unit` per dollar spent.
--   Those were 100 points a night and 3 points per dollar, and they are not comparable
--   until you divide by what the guest paid:
--
--     room:  100 points / $120 (the seeded Standard rate) = 0.83 points per $1
--     F&B:                                        3.00    = 3.00 points per $1
--
--   So a guest earned 3.6x more per dollar ordering than they did for the room they
--   slept in, and the incentive pointed the wrong way: the profitable play was to bill
--   the room and spend nothing, and the room -- the actual product -- was the cheapest
--   way to earn. It also made the category multipliers meaningless, because a Deluxe
--   earns 1.5x the Standard rate (1.25 pts/$) and was STILL out-earned by a coffee.
--
--   The inversion was not visible anywhere. The admin settings screen printed "3" for
--   the order rate and "100" for the per-night rate, and both numbers are individually
--   plausible; the ratio between them is what was wrong, and nothing showed the ratio.
--
-- The fix:
--   `loyalty_accrual_points_per_unit` becomes a FRACTION and drops to 0.5 points per $1,
--   putting F&B at 0.60x what a Standard night earns. The room is now the better prize at
--   every category and every tier, which is the ordering a real programme has.
--
--   The type change is the load-bearing part, and it is why this cannot be a UI change:
--   the code read the setting through `_setting_int()`, so 0.5 truncated to 0 and the
--   order programme would have paid nothing at all. It reads through `_setting_float()`
--   now, and `points_per_dollar_order_vs_room()` is the single place the two rates are
--   compared. tests/test_billing_math.py fails if the ordering is ever reversed again,
--   and the admin screen refuses to save an order rate above the room's own.
--
-- Backfill:
--   The rewrite is guarded on the value still being '3' -- the default that migration 009
--   set. An admin who has already tuned the rate keeps their number, because silently
--   overwriting a deliberate setting is worse than leaving a suboptimal one.
--
--   The stored value goes from an integer to a fraction in the same column. That is safe:
--   `HotelSettings.SettingValue` is NVARCHAR(100) and every reader goes through
--   _setting_float(), which already handles both.
--
-- Safe to re-run (idempotent). Apply from SSMS after migration 023.
USE hotelSystem
GO

UPDATE dbo.HotelSettings
   SET SettingValue = N'0.5'
 WHERE SettingKey = N'loyalty_accrual_points_per_unit'
   AND SettingValue = N'3';
GO
