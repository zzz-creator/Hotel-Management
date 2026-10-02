# PLAN — Per-Night Loyalty Points (Admin-Configurable)

Applies after `PLAN-wire-up-rooms.md` (done). Converts loyalty earning from
order-spend-only to a **per-night** model where room category and tier multiply
the nightly base, and makes **every parameter admin-editable**.

## Design

- **Formula** (awarded at check-out):
  `points = round(nights x loyalty_points_per_night x room_multiplier x tier_multiplier)`
  where `nights = (CheckOutDate - CheckInDate).days`.
- **Base nightly points**: default **100**, stored in `HotelSettings[loyalty_points_per_night]`
  and `config.ini [loyalty] points_per_night`.
- **Category multipliers** (defaults): Standard 1.0, Deluxe 1.5, Junior Suite 2.0,
  Suite 3.0, Grand Suite 4.0, Penthouse 6.0, Presidential Suite 8.0, unknown 1.0.
  Stored as `HotelSettings[loyalty_mult_<slug>]` (e.g. `loyalty_mult_junior_suite`).
- **Idempotency**: `LoyaltyTransactions.SourceID = 'stay:{room}:{check_in}'` guards
  double-award on repeated check-out.
- **Order spend** accrue is unchanged mechanically but its default drops 10 -> 3
  (nights become the primary earner).
- **Tiers rebalanced** for real-world pacing: Bronze 0 / Silver 1000 / Gold 2500 /
  Platinum 7500 (was 5000 / 20000). Tier/points mult/discount already editable;
  **min lifetime points editing added** to the admin Tier Management menu.

## Changes

- [x] `config.ini`: accrual 10 -> 3; add `points_per_night = 100`.
- [x] `migrations/009_loyalty_nightly_points.sql`: insert `loyalty_points_per_night`
      + `loyalty_mult_*` settings (idempotent); guarded accrual `'10' -> '3'`.
- [x] `migrations/010_loyalty_tier_rebalance.sql`: Gold 5000 -> 2500, Platinum
      20000 -> 7500 (guarded against customisations).
- [x] `maincopycopy.py`:
      - Constants: `LOYALTY_POINTS_PER_NIGHT`, `DEFAULT_ROOM_TYPE_MULTIPLIERS`;
        `DEFAULT_TIERS` Gold/Platinum thresholds + descriptions updated.
      - Getters: `get_loyalty_points_per_night()`, `get_room_type()`,
        `_room_type_setting_key()`, `get_room_type_multiplier()`,
        `get_all_room_type_multipliers()`.
      - `award_stay_points(room, check_in, check_out)` — nightly formula,
        idempotent SourceID guard.
      - `check_out()` now calls `award_stay_points()` after flagging the room Dirty.
      - "Pricing & Settings": nightly + order accrual editable (option 4); new
        "Edit Room-Type Points Multipliers" (option 5); View shows nightly base
        and the multiplier table.
      - "Tier Management": new "Edit Tier Minimum Points" (option 3) with
        ascending-order validation.
      - `view_my_loyalty_status()` shows Room Category and effective Points per Night.
- [x] `AGENTS.md`: migrations list, pending-applications order, per-night model,
      new settings keys, tier rebalance note.
- [x] Compiled clean: `py_compile` all four modules + `import maincopycopy`.

## Remaining (after user applies SQL)

- User runs `migrations/008_rooms_seed.sql` then `009` and `010` in SSMS.
- Optional: run "Recalculate All Tiers" (admin) to promote accounts under 010.

## Out of scope

- Database seeds/tweaks beyond the guarded settings inserts.
- Anything security related (plaintext passwords stay).