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

## 9. A declined card after a point redemption — fixed 4 October 2026

**Where:** `bill_room_transactions()` — redemption now at `main.py:5786`, deduction inside
the invoice transaction at `main.py:5874`. `redeem_points_for_invoice()` is the new helper.
**Severity:** was a real money defect. Fixed.

Two facts combined badly. `redeem_points_by_customer()` committed immediately and was the
**only** loyalty mutation in the app with **no `SourceID`** — so it had no idempotency guard
and no way to be detected or reversed after the fact. And the redemption was *not* part of
the transaction that writes the invoice: it ran before the card prompt, and the card prompt
is a blocking `input()` that cannot sit inside a transaction.

So the sequence was: guest redeems 500 points → the balance drops and a
`LoyaltyTransactions` row is committed → the card declines → `bill_room_transactions()`
returns `False` having written nothing. The guest had lost the points and still owed the
entire bill. Re-running check-out did not recover them, because the balance was already
reduced.

**Measured before the fix, not inferred.** A probe against a disposable database — driving
the real `check_out()` with a redemption of 500 points and a card that fails the Luhn check
— reported `points before=2600 after=2100 | redemption ledger rows=1 | invoices=0`.

**Fixed by deferring the deduction.** The prompt still happens where it did, but it now only
records the *intent*; the points come off in the same transaction that inserts the invoice,
which is exactly what `apply_booking_credit()` (`main.py:5810`) already did for the same
reason. The deduction carries `SourceID = 'redeem:{invoice_id}'`, so it is self-checking in
the same way as `award_stay_points()`'s guard, and a repeat is detectable rather than
silent.

`redeem_points_for_invoice()` **raises** `LoyaltyRedemptionError` rather than returning
`False` when the balance will not cover the redemption. This is deliberate and is the one
place the two patterns differ: `apply_booking_credit()` logs and returns 0, but the invoice
snapshot here *claims* the discount in `PointsRedeemed`/`RedemptionValue`. A silent `False`
would let that claim commit against a balance nobody reduced. Raising rolls the invoice back,
leaving the guest unbilled and uncharged so the attempt can be repeated.

`verify_e2e.py` Phase 5c covers all three outcomes — a declined card leaving the balance and
the ledger untouched, a settled redemption deducting exactly once with a `SourceID` and an
agreeing invoice, and a repeat run taking nothing further. The assertions were proven red
against the old code first, which reported `2600 -> 2100, with nothing billed`.

Do **not** "fix" this differently by moving the card prompt inside a transaction, or by
removing the declined-card retry — see [BOOKING.md §6](BOOKING.md) on why the room charge
posts before payment on purpose.

---

## 10. A failed check-out leaves residue — now reported, not prevented

**Where:** `check_out()` steps at `main.py:6308-6319`; `settlement_outstanding()` at
`main.py:6177`, `announce_settlement_outstanding()` at `main.py:6238`.
**Severity:** the residue was real and silent. Now reported. The *prevention* is still a
deliberate non-goal — see below.

Check-out is five separately-committing steps, not a transaction, because
`bill_room_transactions()` prompts for card details and that prompt cannot sit inside a
transaction. So a failure part-way through leaves a genuine half-finished stay. Until
4 October 2026 nothing said so: `check_out()` returned `None` on success and on failure
alike, and reported only the exception that caused the failure. A clerk was left to infer
whether the guest was still in the room, still holding a working key card, and still owed
money.

**The residue is now enumerated rather than described.** Phase 5e injects a failure at each
of the five steps and measures what is left, so this list is observed, not reasoned:

| failure at | left behind |
|---|---|
| `post_room_charge` | no invoice; 2 unbilled charges; live key card; room still `Occupied`; no stay points |
| `bill_room_transactions` | no invoice; 3 unbilled charges; live key card; room still `Occupied`; no stay points |
| `set_room_status` | live key card; room still `Occupied`; no stay points |
| `revoke_active_key_cards` | live key card; no stay points |
| `award_stay_points` | no stay points |

`settlement_outstanding()` derives this from the rows, not from memory, because the process
that failed is gone by the time anyone asks — a guest who has already walked out leaves
nothing to interrogate. `announce_settlement_outstanding()` prints it, and `check_out()`
calls it on both failure paths.

### Why the steps were not reordered instead

An advisory review of this codebase proposed moving `revoke_active_key_cards()` and
`set_room_status()` **before** payment, on the grounds that a failure after a successful
card leaves a departed guest with live door access. That is a real defect, and it is the
sharpest entry in the table above.

The reorder was **not** applied, because it trades this residue for a worse one rather than
removing it. `post_room_charge()` runs first *specifically* so a declined card can be
retried (`main.py:6304-6307`); the same reasoning applies with more force to key revocation.
Revoke first and a declined card leaves a guest who has not paid, is still in the room, and
now cannot open their own door — with the bill still unsettled. There is no ordering of five
non-atomic steps that leaves nothing behind; the only question is which residue you would
rather be told about.

That is the argument for reporting rather than reordering: **make the residue visible instead
of choosing a different residue and hoping.** If you do revisit this, the ordering change is
worth making *together with* a recovery path for whatever it leaves behind, not instead of
one.

Phase 5e also asserts the negative direction, which matters more than the positive one: a
**completed** check-out reports nothing outstanding. A signal that fires on healthy rooms is
worse than no signal, because it teaches staff to ignore it.
