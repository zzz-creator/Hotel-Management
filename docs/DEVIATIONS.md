# Known deviations

Things this app does **not** do, and does not do correctly, on purpose or because the fix
is larger than the defect. Each entry says what the behaviour is, why it is like that, and
what would actually close it.

This file exists so the next person to hit one of these does not spend a day
re-diagnosing it — and, more importantly, does not "fix" it by accident. Several entries
are load-bearing decisions rather than bugs; the section heading says which.

Nothing here is a surprise that the tests do not already cover. If a behaviour below ever
changes, the change belongs in this file in the same commit.

---

## 1. `Reservations.RoomNumber` is the primary key

**Where:** `docs/SCHEMA.md` §4. **Severity:** structural.

A room can hold at most one stay row, ever. Overlapping stays are not representable, and
re-letting a room overwrites the previous stay's live row (it is copied to
`ReservationArchive` first, which is what preserves history).

**Fix:** an append-only `StayLedger` — one row per stay with a surrogate key, so a room
holds many non-overlapping stays.

**Why it is not done:** it rewrites `_book_reservation_in_conn()`, whose transaction
handling is the one unambiguously correct thing in this codebase — it claims the room,
archives the outgoing stay and writes the incoming one on a single connection with **no
commit**, so a declined card cannot leave a room reserved. Replacing that as a side effect
of a schema change, with no live-database test of the book → check out → cancel path, is
how a working booking system stops booking.

---

## 2. `AuditLog` records actions, not diffs

**Where:** `log_audit()`. **Severity:** forensic.

`log_audit("UPDATE", "Reservation", "9012", "…")` stores the free-text detail the caller
passed and nothing else. There is no before-image, so the log cannot answer "what was this
rate before Tuesday?" — only "something about a rate happened on Tuesday, and here is the
sentence the code wrote".

**Fix:** read the old row before the write and pass both values, or add `OldValue` /
`NewValue` columns and have `log_audit()` populate them. The first is a per-callsite change
in ~40 places; the second is a migration plus a signature change.

**Why it is not done:** the callers are the only ones that know what the old value was, so
this cannot be fixed centrally without either changing every callsite or re-reading the row
inside `log_audit()` — and the latter is wrong for a re-let, where the "old row" has
already been archived or overwritten by the time the audit call happens.

---

## 3. The business date is a clock, not a night audit

**Where:** `business_date()`, `close_day()` (023). **Severity:** by design.

`close_day()` moves `HotelSettings['business_date']` forward one day. It does **not** post
the day's folios, roll occupancy, expire anything, clean rooms, or re-read rates. Reports
are therefore *re-runnable for a chosen day* (same input, same numbers) but occupancy is
**derived live** from `Reservations` rather than accumulated — so editing a past stay
changes what that past day's report says. There is no stored daily fact to compare against.

**Fix:** a nightly snapshot table written by a real audit job (occupancy per night, folio
totals per day, a room-status roll-forward). That is a business-objects layer this app does
not have.

**Why it is not done:** calling the current behaviour a night audit would be a lie, and
building the real one piecemeal would be worse. The distinction is load-bearing: the setting
is a clock, and the docs say so in both places.

---

## 4. Loyalty points never expire

**Where:** 025. **Severity:** by design.

`loyalty_expiration_days` used to be seeded at `0` ("0 = never expire") and editable from
the admin settings screen — and read by **nothing**. It was a control that looked exactly
like a control that worked; setting it to 90 would have been accepted, stored, displayed,
and changed no behaviour. Migration 025 deletes it, because a knob that does nothing is
worse than a missing feature: its only failure mode is invisible.

Points are kept forever, and recency is rewarded through tier promotion rather than a time
limit.

**Fix, if expiry is ever wanted:** a real expiry sweep keyed on `LoyaltyTransactions.CreatedAt`
with a `LoyaltyAccounts.Points` recompute, plus a disclosure the guest can see. Not a
setting.

---

## 5. Report figures are settlement-day, not accrual

**Where:** `export_occupancy`, `export_revenue`. **Severity:** by design.

ADR and RevPAR attribute room revenue to the night the **invoice was issued**, not to the
night it was earned. A three-night stay checked out on the 4th puts all three nights' revenue
on the 4th. The column headers say `RoomRevenue`, and the numbers are internally consistent
and reproducible — they are simply not a per-night accrual.

**Fix:** spread `NightlyRate × nights` across the nights of the stay at invoice time, which
means a per-night revenue table.

**Why it is not done:** the current figure is defensible and correctly labelled; an accrual
version would disagree with it on every historical month and needs the nightly snapshot from
§3 to be meaningful.

---

## 6. All stay windows are half-open, and one report used to disagree

**Where:** `stays_overlap()` (the single source of truth), `export_housekeeping`. **Severity:**
fixed.

A stay is `[CheckInDate, CheckOutDate)`, so a guest departing on the 4th does **not** occupy
the night of the 4th. `export_occupancy` and `search_availability()` used this rule;
`export_housekeeping` used `CheckOutDate >= GETDATE()` and so reported a guest as in house on
the night they left — the two reports answering differently about the same row, with nothing
anchoring either of them. Both now use the half-open window, and the board takes a date so a
closed day can be re-run.

Note the remaining limit from §1: "current guest" on the board is the room's **live** stay, so
a re-let room shows its current occupant and never a past one.

---

## 7. F&B earns less per dollar than the room — calibrated, not arbitrary

**Where:** `get_loyalty_accrual_points_per_unit()`, `points_per_dollar_order_vs_room()`
(024). **Severity:** by design, and enforced.

The order rate is a **fraction** (0.5 pts/$), read as a float. It was 3 pts/$ against
100 points per night, which on a $120 Standard room meant a guest earned **3.6× more per
dollar** ordering than for the room they slept in — the incentive pointed the wrong way and
the category multipliers were meaningless, since even a Deluxe could not catch up.

**Fix:** none needed. `points_per_dollar_order_vs_room()` is the calibration, the settings
screen shows both rates side by side, and `tests/test_billing_math.py` fails if the ordering
inverts. The value must stay a float: read as an int, `0.5` becomes `0` and every order pays
nothing.

---

## 8. Plaintext passwords

**Where:** `Users.Password`, `CustomerProfiles.Password`, `config.ini` `[hotel] master_secret`.
**Severity:** by design (teaching/demo project).

Passwords are stored and compared in plaintext throughout. Do **not** introduce hashing or
salting without being asked — see AGENTS.md §3. This is the one entry here that is
knowingly insecure rather than merely incomplete, and it is the reason `require_master_override()`
has a plaintext fallback to the `master` account.

---

## 9. A declined card after a point redemption burns the guest's points

**Where:** `bill_room_transactions()` — redemption at `main.py:5726`, card prompt at
`main.py:5756`. **Severity:** real defect, money. Not yet fixed.

Two facts combine badly. `redeem_points_by_customer()` (`main.py:912`) commits immediately
and is the **only** loyalty mutation in the app with **no `SourceID`** — so it has no
idempotency guard and no way to be detected or reversed after the fact. And the redemption is
*not* part of the transaction that writes the invoice: it runs before the card prompt, and the
card prompt is a blocking `input()` that cannot sit inside a transaction.

So the sequence is: guest redeems 500 points → the balance drops and a `LoyaltyTransactions`
row is committed → the card declines → `bill_room_transactions()` returns `False` having
written nothing. The guest has lost the points and still owes the entire bill. Re-running
check-out does not recover them, because the balance is already reduced.

**Measured, not inferred.** A probe against a disposable database — driving the real
`check_out()` with a redemption of 500 points and a card that fails the Luhn check — reported
`points before=2600 after=2100 | redemption ledger rows=1 | invoices=0`. The probe was removed
after confirming the defect, because a permanent assertion here would be a permanent failure.
`verify_e2e.py` Phase 5b answers "no" to the redemption prompt for the same reason.

**Fix:** defer the redemption until payment has succeeded, and apply it in the **same
transaction that writes the invoice** — the pattern `apply_booking_credit()` already uses at
`main.py:5810` for exactly this reason ("consume the credit in this same transaction, so a
rolled-back invoice leaves the credit available for the retry"). The redemption prompt can stay
where it is; only the deduction moves. It should also gain a `SourceID`, so a retry is
detectable even after the fact.

Do **not** fix this by moving the card prompt inside a transaction, or by removing the
declined-card retry — see [BOOKING.md §6](BOOKING.md) on why the room charge posts before
payment on purpose.
