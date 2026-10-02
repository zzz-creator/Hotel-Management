# Plan: Public Booking System

Status: **delivered, migration 018 not yet applied.**

## Goal

Let a guest reserve a room and pay before arriving, without phoning the front desk, and
let them cancel for a refund. Reachable from the main screen as **3. Bookings**, with no
login — a guest who has not checked in has no room number or `Users` account to
authenticate with.

## The two requests, and why they are separate

1. `validate_room()` stopped listing every stay that shared a surname. The old prompt
   fetched all reservations for the typed last name and printed each one's room number
   and first name, then asked the guest to pick a number. A surname is not unique, so
   that list was a directory of who was currently in the hotel, disclosed to anyone who
   could guess a common name.
2. The booking desk (this document).

They share one theme: **stop treating a surname as an identifier.**

## Decisions taken

| Question | Decision |
|---|---|
| Where does booking live? | Main menu `3. Bookings`, no login (Exit moves to `4`). |
| Does the guest pick a room? | No — room **type** only. The app assigns the first available room of that type. |
| What is the reference? | The assigned room number, plus a `BK-` booking reference. |
| When is money taken? | Up front, via the existing simulated `process_credit_card()`. |
| How much up front? | A **mandatory** one-night deposit, or the full amount. No pay-at-check-out. |
| Cancellations? | Full refund outside a cutoff, deposit forfeited inside it, **refused from the check-in date onwards**. |
| Is the quoted rate guaranteed? | **No — it is an estimate.** The final bill is priced at check-out. |
| How is a cancelled stay recorded? | A signed money row. **Not** an archive row. |

## Schema — `migrations/018_booking_payments.sql`

### `ReservationPayments`

A separate table from `Transactions` **on purpose**. `Transactions` is the guest folio;
putting a booking deposit there would make it look like hotel revenue — inflating
`Transactions.Amount`, the `Invoices` subtotal, and therefore ADR/RevPAR and the revenue
report, even though the same money is deducted again at check-out. Booking money arrives
before there is a folio, and it must survive the deletion of the reservation it was for.

**Rows are signed.** `Deposit`/`Prepayment` are positive; `Refund`/`Forfeit` are
negative, so a stay's net position is a single `SUM(Amount)`. `Forfeit` is a separate
`Kind` from `Refund` purely so reporting can distinguish money returned from money kept —
both zero the same net.

`Kind` is CHECK-constrained to those four values, so a typo cannot invent a money
movement the UI cannot render.

**`StayCheckIn` identifies the stay, not just the room.** Rooms are re-let after every
check-out, so keying on `RoomNumber` alone would let a previous guest's credit leak onto
the next guest's bill. `IsApplied` + `AppliedToInvoiceID` then mark a credit as consumed
exactly once, so a retried check-out after a declined card cannot double-claim it.

Only the card's last four digits are stored, in `CardLast4`; `process_credit_card()`
records them in a module global and never keeps the PAN or CVV.

No FK to `Reservations`, matching `Transactions`, `Invoices`, `KeyCards` and `DoorEvents`
— room numbers are free-text `<floor><3-digit-code>` and the schema deliberately leaves
them unconstrained.

### `Invoices.PrepaidAmount`

Records the credit actually applied to a check-out invoice, so the invoice is
self-describing. `_build_invoice_insert()` omits the column when it does not exist
(`_invoices_have_prepaid_column()`, cached), so **check-out keeps working before 018 is
applied** rather than failing with an invalid-column error.

## Money rules (pure, unit-tested in `tests/test_booking.py`)

- **`booking_quote(nights, rate, tax)`** — room subtotal + its tax. It mirrors the
  check-out folio exactly, so a full prepayment covers the room bill precisely. Inputs
  are clamped, so a mistyped rate can never produce a negative quote shown to a guest.
- **`booking_payment_options(...)`** — a **deposit is required to secure the room**, so
  there is no pay-at-check-out option. It offers a deposit capped at the stay length (a
  one-night stay is never asked for a second night), then pay-in-full. This is also what
  guarantees every confirmed booking is backed by a `ReservationPayments` row, which is
  what `view_my_booking()`/`cancel_booking()` join on. The list is never empty, so the
  wizard's `ask_number(minimum=1, maximum=len(options))` stays satisfiable.
- **`settle_with_prepayment(total, credit)`** — credit is capped at the total. A guest who
  prepaid against a higher rate ends up with `balance_due`; one who prepaid against a
  lower rate ends up with `credit_unused` for a later refund. A negative bill is not
  representable.
- **`refund_decision(days_until, cutoff, paid)`** — `refused` once `days_until <= 0`
  (the check-in date itself, not just after it), `none` when nothing was paid, `refund`
  at least `cutoff_days` ahead, else `forfeit`.
- **`booking_refund_policy(check_in, cutoff, amount)`** — the guest-facing sentence, shown
  in a `Cancellation Policy` box *before* the payment options and again in the booking
  confirmation with the exact amount at risk. A guest cannot be held to a non-refundable
  deposit unless they were told first. The deadline is `check_in - max(cutoff, 1)` days,
  derived from the same `days >= cutoff` rule `refund_decision()` applies; the `max(..., 1)`
  is load-bearing because the arrival date is refused, so a naive "cutoff days ahead"
  deadline would promise a refund that is then refused. A unit test walks every day from
  the stated deadline to the arrival date to keep the wording and the code in agreement.

`booking_refund_cutoff_days` is a `HotelSettings` key (default 7) editable from
"Pricing & Settings" → "Edit Booking Cancellation Policy", read through `_setting_int()`
so a typo falls back to the default instead of breaking cancellation.

## Why the quote is only an estimate

Rates are admin-editable, and F&B is added at check-out, so no upfront figure could
honourfully be final. The guest is told the total is an estimate, and
`settle_with_prepayment()` reconciles the difference either way. Locking the booking rate
would need a rate snapshot column on the reservation; that was considered and rejected
as more machinery than the demo needs.

## The race-safe room assignment

`Reservations.RoomNumber` is the primary key, so a room holds at most one reservation
row. `book_room()`:

1. Reads candidates with `search_availability()` **before** asking for a card, so a
   sold-out type fails fast without requesting payment details.
2. Claims a room with `_book_reservation_in_conn()`, which applies the same rules as
   `add_reservation()`: a stay that has not finished refuses the claim; a completed past
   stay is archived and then overwritten.
3. On a collision — another desk took the same room between the read and the insert —
   catches it and tries the next candidate.

**Payment happens before the commit**, inside the same transaction. A declined card rolls
the whole booking back, so a declined card never holds a room. Archiving
(`_archive_row()`) also runs on that connection without committing, so a failed booking
cannot leave a duplicate history row behind.

## Why a cancelled booking is not archived

`ReservationArchive` feeds `search_availability()`. Archiving a stay that never happened
would mark the room busy for exactly the dates it was freed up for — the cancellation
would block its own rebooking. The signed `Refund`/`Forfeit` row is the cancellation's
history. The `DELETE` is additionally guarded on the same `CheckInDate`/`CheckOutDate`
that were quoted, so a desk-side re-book cannot be cancelled out from under the next
guest.

## Check-out integration

`bill_room_transactions()` reads the stay's outstanding credit via
`_reservation_check_in()` → `get_outstanding_booking_credit()` and applies it **after**
loyalty redemption, so it reduces the card charge rather than the loyalty entitlement.
`apply_booking_credit()` marks the payments consumed inside the same transaction as the
invoice insert, so the credit and the invoice either both land or neither does — a
declined card leaves the credit available for the retry.

## `validate_room()` rewrite

Last name + first name + room number, matched in **one** query; `normalize_room_number()`
so `01001` matches `1001`; `VALIDATE_ROOM_MAX_ATTEMPTS` (3) tries; a database fault
returns immediately rather than retrying. The "not found" message deliberately does not
distinguish "unknown surname" from "wrong first name", so the prompt cannot be used to
discover who is staying. When one person holds two stays, the room number disambiguates
in SQL — there is no list to choose from.

## Two money bugs found while shipping the desk

Both were found by re-reading the money paths after the first end-to-end run, and both are
now migrations with tests rather than code comments.

### 1. Credit was consumed whole-row (`020_partial_booking_credit.sql`)

`apply_booking_credit()` set `IsApplied = 1` on **every** unspent payment row for the stay.
`IsApplied` is a BIT, so "used" could only ever mean the whole row — but a booking quote is
an estimate, and the final bill is priced at check-out. An admin cutting a rate after the
guest prepaid makes the bill *smaller* than the prepayment, and then a $250 bill against a
$300 prepayment was recorded as $300 of credit, with $50 reported to the guest as "unused"
while the ledger insisted it was spent. The invoice and the payment rows disagreed, and
there was no way to express the truth.

`AppliedAmount` records how much of a row went to invoices; outstanding credit is
`SUM(Amount - AppliedAmount)`; `allocate_booking_credit()` (pure, unit-tested) splits a
bill's credit across rows **oldest first** so the surplus sits in the most recent payment.
`IsApplied` is kept and still flips the instant a row is fully consumed, so nothing that
filters on it changes meaning, and the settled-once guarantee survives.

### 2. The booking reference was not actually unique (`021_booking_ref_uniqueness.sql`)

`new_booking_ref()` generated six random hex digits and asked `booking_reference_exists()`
whether it was free. That probe ran on **its own connection, before** the booking
transaction opened, and nothing in the schema stopped two sessions from both deciding the
same reference was free. A reference is the only handle a guest has on their own booking,
so a shared reference means one guest can read — and cancel — the other's stay, which
destroys the reservation *and* pays out its deposit.

The index is filtered to `Deposit`/`Prepayment` because a cancellation writes a second row
with the *same* reference, so one booking legitimately owns several rows. It is keyed on
the **payment rows** rather than on `Reservations`, and that choice is the security
property: cancelling deletes the reservation, so keying on the reservation would free the
reference and `new_booking_ref()` could reissue it — handing a guest who still holds an
old cancelled reference someone else's booking. Payment rows are kept precisely because
money history must outlive the reservation.

A collision is a race, not a failure, so `record_booking_payment()` raises
`BookingRefTaken` and `_write_booking_charge()` retries with a fresh reference rather than
telling the guest their booking failed. `_is_duplicate_key_error()` is deliberately narrow:
it matches 23000/23505/2601/2627 and *not* the whole SQLSTATE class 23, because that class
also covers FK and CHECK violations, which would be retried five times and then reported as
"could not find a free booking reference".

## Guest accounts — `migrations/019_customer_identity.sql`

A booking is only findable and cancellable by a reference that is 8 characters, and a
loyalty balance is only meaningful if it belongs to a person. Both need a guest account,
so `019` adds one and the desk now requires a sign-in.

- **Email + password.** `CustomerProfiles.Password` is plaintext, matching
  `Users.Password` (this is a teaching project — see AGENTS.md). `Email` carries a unique
  **filtered** index (`WHERE Email IS NOT NULL`) because `check_in()` writes name-only
  profiles that leave `Email` NULL, and SQL Server permits only one NULL under a plain
  unique index.
- **Register on first use.** An unknown email is offered a new account rather than
  rejected: a guest who cannot create an account cannot book, and a guest who cannot book
  has no way to be recognised on a return visit. `CUSTOMER_LOGIN_MAX_ATTEMPTS` (3) caps
  guessing, because the booking menu is public.
- **The booking is owned.** `_book_reservation_in_conn()` writes `Reservations.CustomerID`,
  and `book_room()` takes the guest's name from the account instead of asking again. One
  account, one name — otherwise a stay's loyalty could be read under a name the guest
  does not sign in with.
- **Lookup and cancellation are restricted to the owner.** `_find_booking()` takes a
  `customer_id` and filters on it; `cancel_booking()` also puts `AND CustomerID = ?` on
  its `DELETE`. Guessing an 8-character reference would otherwise let a stranger read
  your room and cancel your reservation, which destroys the stay *and* pays your deposit
  to them. The "not found" message never distinguishes a wrong code from someone else's
  code, because confirming a guess would disclose that the guest exists.
- **Legacy and front-desk bookings are staff business.** A stay with no linked owner can
  never match the guest-side lookup. `check_in()` calls `link_reservation_customer()` so
  the next stay does get a link.
- **Same migration, same theme.** 019 also re-keys `LoyaltyAccounts` from `RoomNumber` to
  `CustomerID`; see AGENTS.md for why a room-keyed balance was handing the next guest of
  a room the previous guest's points and tier.

## Verification

- `python -m py_compile main.py db.py reports.py ui.py`
- `python -m unittest discover -s tests` — 211 tests
- `python tests/check_schema_sync.py` — 20 comparisons, 0 issues (reads 019/020/021's
  `ALTER`s and `LoyaltyAccounts` PK re-key, which a `CREATE TABLE`-only pass misses, and
  compares every UNIQUE index a migration creates — a filtered index is a correctness
  guarantee, so one silently absent from `database.sql` would give a fresh install
  different behaviour from a migrated one)
- `tests/test_booking.py` pins the arithmetic and the **sign** of every money movement,
  plus the invoice SQL in both pre-018 and post-018 shapes, the partial-credit split, the
  retry-on-collision behaviour, and the card a reversal is attributed to.
- `tests/test_schema_sync.py` tests the checker itself, by feeding it deliberately broken
  SQL. A guard that reports 0 issues because its parser stopped matching is worse than no
  guard at all, so that failure mode is pinned too.
- `tests/test_customer_loyalty.py` pins that a balance follows the person (two rooms of
  one guest read one balance; two guests of one room read two), that the award
  `SourceID`s are guest-scoped, that a re-let clears the outgoing guest's `CustomerID`,
  and that login/ownership behave at the boundaries.
- `tests/test_validate_room.py` asserts the query carries both names and that no other
  guest's room number or first name can reach the output.

## Apply order

`migrations/013` through `migrations/021`, in order, in SSMS. Until 018 is applied a
deposit cannot be recorded: the app says so rather than pretending the money was taken,
and check-out bills in full.
