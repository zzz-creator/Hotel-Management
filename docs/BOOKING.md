# docs/BOOKING.md

The public booking desk (`booking_panel()` on the main menu, "3. Bookings", **no staff
login**) and the customer-keyed loyalty program.

**Read this before** touching `book_room()`, `view_my_booking()`, `cancel_booking()`,
`customer_login()`, anything under the loyalty section of `main.py`, or
`reports.py`'s loyalty export. The rules here exist because each one has a real defect
behind it; "fixing" one typically reintroduces that defect.

Related: [SCHEMA.md](SCHEMA.md) (tables, migrations, degradation) ·
[../AGENTS.md](../AGENTS.md) (conventions, workflow, verification commands)

---

## 1. Guest identity

`customer_login()` sets the module-level `CURRENT_CUSTOMER` (a `CustomerID`; `None` = signed
out) and is **cached until Sign Out**. The Bookings and Customer menus offer Sign Out, which
clears it; within one signed-in session it is never re-prompted. `book_room()`,
`view_my_booking()` and `cancel_booking()` all call it first.

- An unknown email **registers on first use** rather than being turned away: a guest with
  no account cannot book, and therefore has no way to be recognised on a return visit.
- Blank email or blank password is refused — both are the only handle the account has, and
  a booking always has an owner.
- The `customer_login_max_attempts` `HotelSettings` key (default 3) caps password
  guessing. This is a **public** menu; it must not be an unlimited oracle.
- The login lookup is by email, which is why the filtered unique index
  `UX_CustomerProfiles_Email` is load-bearing — see SCHEMA.md §2.

**A booking is owned.** `_book_reservation_in_conn()` takes the `customer_id` and writes it
to `Reservations.CustomerID`; `book_room()` then takes the guest's name from the *account*
rather than re-asking for free text. A second name on one account would let a stay's
loyalty be read under a different name than the one the guest signs in with.

**A booking always has a payment row, and that is load-bearing.** `view_my_booking()` and
`cancel_booking()` find a booking by joining `Reservations` to `ReservationPayments` on
`BookingRef` + `StayCheckIn`, so a booking with no `ReservationPayments` row could be
neither viewed nor cancelled. Requiring a deposit guarantees the row. So `book_room()`
writes the payment row even when it is `$0.00` (a zero-rated room), skipping only the
pointless card prompt — and `booking_payment_options()` must **never return an empty list**,
which would leave the wizard's `ask_number(minimum=1, maximum=0)` impossible to satisfy.

### Privacy rules

- `_find_booking(booking_ref, customer_id)` **restricts the lookup to the signed-in guest**,
  and `cancel_booking()` additionally puts `AND CustomerID = ?` on its `DELETE`. A booking
  reference is 8 characters and is brute-forceable; without both checks anyone who guessed
  one could read a stranger's room and cancel their reservation — which would destroy the
  stay *and* pay out their deposit to them.
- The "not found" message **deliberately does not distinguish** "no such code" from
  "someone else's code". Confirming a guess would disclose that the guest exists.
- A booking with no linked owner can never match, so legacy and front-desk bookings are
  front-desk business. `_own_booking()` says so.
- `validate_room()` (the *in-house* guest's identity check, used by every other
  guest-facing feature) requires **last name + first name + room number** in one query, with
  `VALIDATE_ROOM_MAX_ATTEMPTS` (3) tries. It must never print a list of other stays for a
  surname: a last name is not unique, and the old listing disclosed other guests' room
  numbers and first names to anyone who knew a surname. When one person holds two stays the
  room number disambiguates, so the query is narrowed **in SQL** rather than offered as a
  menu, and the not-found message does not distinguish "unknown surname" from "wrong first
  name". Room numbers are compared through `normalize_room_number()`, so `01001` matches
  `1001`.

---

## 2. The booking wizard

A guest books by room **type**; the app assigns the first available room of that type via
`search_availability()`. **The room number the guest receives is their booking reference.**

The rate is a **contract, not an estimate**. `Reservations.NightlyRate` (022) captures the
per-night rate at the moment the stay is made, and `post_room_charge()` bills that number —
so an admin rate edit after booking does not re-price a confirmed stay, in either
direction. Only a stay with no captured rate (written before 022, or a category with no
rate) falls back to the current rate, and check-out says so out loud. `NULL` is that
signal, and is never `0`.

The *quote* is still an estimate in one specific sense, and only that one: **F&B is not in
it yet**. The room portion is fixed from the moment of booking; the final total grows with
whatever the guest orders. Booking money therefore still has to be able to come back out
*under* what was taken (see §4) — but because of refunds, not because the room rate moves.

**The room category is re-read inside the booking transaction.**
`_book_reservation_in_conn()` takes the `room_type` the guest asked for and re-reads it via
`_room_type_in_conn()` — the in-connection twin of `get_room_type()`, which opens its own
connection and so cannot be used mid-transaction. The candidate list came from
`search_availability()` *before* the transaction opened, and an admin can re-type a room in
the gap; a guest who asked for a Deluxe must not silently be given a Standard. A room with
no `Rooms` row is "cannot verify", not "wrong type", and is allowed — refusing there would
break front-desk and legacy rooms.

**A declined card holds no room.** The payment is taken before the commit and the whole
booking rolls back. A `$0.00` booking is the one case that commits without a card prompt,
because there is nothing to decline.

---

## 3. The money rules (pure, unit-tested)

All four live in `main.py` with no database access, and are covered by
`tests/test_booking.py`. **Keep them pure** — that is what makes the money testable.

**`booking_quote(nights, nightly_rate, tax_rate)`** → `{nights, nightly_rate, tax_rate,
subtotal, tax, total}`. Mirrors the check-out folio exactly: the room group is taxed, and
F&B is not yet in play, so a full prepayment here covers the room bill precisely. Inputs
are **clamped, not rejected**, so a stray setting value can never produce a negative quote.

**`booking_payment_options(nights, nightly_rate, tax_rate)`** → list of
`(choice, label, kind, amount)`. **A deposit is mandatory to secure the room**, so there is
no pay-at-check-out option — only a one-night deposit (capped at the stay length, so a
one-night stay is never asked for a second night up front) or pay-in-full. Never empty; see
§1.

**`settle_with_prepayment(total, credit)`** → `{total, credit_available, credit_applied,
credit_unused, balance_due}`. Credit **never exceeds the bill**; the surplus is reported as
`credit_unused` (real, refundable money) rather than becoming a negative balance.

**`refund_decision(days_until_check_in, cutoff_days, amount_paid)`** → `{action, kind,
amount, reason}` where action is `refused` / `none` / `refund` / `forfeit`. Both `refund`
and `forfeit` write a **negative** row so a stay's payments always net back to zero; `Kind`
is what distinguishes money returned from money kept.

### `refund_decision()` refuses at `days_until_check_in <= 0`, not `< 0`

This is load-bearing, not a rounding nicety. `cancel_booking()` **DELETEs** the
`Reservations` row, so allowing a cancellation on the arrival date would wipe an in-house
stay, mark the room `Available` on the housekeeping board while the guest is in it, and
leave check-out with no reservation to bill against. The guest is told to contact the front
desk. `_find_booking()` only filters `CheckOutDate >= today` (the business date), so an
in-house guest *is* reachable — this refusal is the only thing stopping the delete.

`booking_refund_cutoff_days` is an admin setting, read through `_setting_int()` so a typo
falls back to the default.

### The refund policy is stated at booking time, in two places

A guest cannot be held to a forfeiture they were never told about.
`booking_refund_policy(check_in, cutoff_days, amount_charged=None)` renders one sentence
naming the free-cancellation deadline, the forfeiture consequence, and the "from check-in
onwards, contact the front desk" rule. It is shown in a `Cancellation Policy` box **before
the payment options** (so the guest is told before handing over a card) and repeated in the
booking confirmation with the exact amount at risk.

The deadline is `check_in - max(cutoff, 1)` days, **derived from the same `days >= cutoff`
rule `refund_decision()` applies** rather than restated by hand. The `max(..., 1)` matters:
the arrival date itself is refused, so a naive "cutoff days ahead" deadline would promise a
refund that is then refused. A unit test walks every day from the stated deadline to the
arrival date to keep the wording and the code in agreement — change one and that test is
the thing that tells you.

### Booking references are unique by database guarantee

`new_booking_ref()` mints `BK-` + six hex digits. It used to *probe*
`booking_reference_exists()` for a free reference, but that probe ran on its **own
connection, before** the booking transaction opened, so two sessions could both pass it and
share a reference — and the reference is the only handle a guest has on their own booking.

Migration 021 enforces it with a filtered UNIQUE index over the two kinds that appear
**once per booking** (`Deposit`/`Prepayment`); a cancellation writes a *second* row with the
*same* reference, which is why a plain UNIQUE on the column is impossible. The index is
keyed on the payment rows rather than on `Reservations` **on purpose**: cancelling DELETEs
the reservation but deliberately keeps the payment rows, so keying on the reservation would
free the reference and let `new_booking_ref()` reissue it to a new guest — handing a guest
who still holds an old cancelled reference someone else's booking.

`record_booking_payment()` raises `BookingRefTaken` on a unique violation, and
`_write_booking_charge()` retries with a fresh reference (bounded by
`BOOKING_REF_WRITE_ATTEMPTS`) instead of failing the guest. It returns `None` only for a
real failure the guest should hear about.

**`_is_duplicate_key_error()` must stay narrow.** It matches 23000 / 23505 / 2601 / 2627
and deliberately **not** the whole SQLSTATE class 23, because class 23 also covers FK
(23503) and CHECK (23512) violations. Treating those as a taken reference would retry a
doomed insert and then report "could not find a free booking reference" instead of the real
error. It reads codes from both `args` and the message text, because pyodbc sometimes
delivers them as one stringified tuple, and a classifier that silently answered `False` for
a real duplicate key would spin the retry loop and report the wrong reason.

### A reversal names the card that took the money

A `Refund`/`Forfeit` row reads its `CardLast4` back from the booking's own initial charge
via `_original_charge_card(cursor, booking_ref)`, **never** from `LAST_CARD_DIGITS`. That
global is the last card the *process* authorised, and a cancellation never prompts for a
card, so it still holds whatever an unrelated guest paid with — a refund row would otherwise
name a stranger's card.

### A cancelled booking is deliberately NOT archived

`ReservationArchive` feeds `search_availability()`, so archiving a stay that never happened
would mark the room busy for dates nobody occupied. The signed `Refund`/`Forfeit` row is
the cancellation's history.

---

## 4. Booking credit at check-out

**Partial credit is recorded per row, not per flag** (migration 020). The room rate is now
captured at booking (022), so an admin cut can no longer shrink the room bill — but a bill
can still be *smaller* than the prepayment, because a guest who prepaid three nights and
stayed one has a refund coming. `IsApplied` is a BIT and could only say "whole row spent" —
the old `apply_booking_credit()` marked every unspent row consumed, so a $250 bill against a
$300 prepayment was recorded as $300 of credit with $50 silently gone.

`ReservationPayments.AppliedAmount` now records the portion of a row given to invoices, so
**outstanding credit is `SUM(Amount - AppliedAmount)`** and the remainder stays real and
reportable. `IsApplied` is kept as the cheap whole-row flag and still flips the moment a row
is fully consumed, so nothing that filters on it changes meaning. Two-statement writes mean
there is deliberately **no** CHECK tying `IsApplied` to `AppliedAmount`; a range CHECK
(`CK_ReservationPayments_AppliedAmount`) does exist.

**`allocate_booking_credit(payments, amount)`** is the pure allocator: it consumes
**oldest row first**, so the surplus is left in the guest's most recent payment. A row may
be only partly consumed, and the returned `[(PaymentID, consumed)]` is what makes that
representable. It is unit-tested without a database.

`apply_booking_credit()` runs **inside the caller's transaction** alongside the invoice
insert, so the credit and the invoice either both land or neither does. It no-ops when there
is nothing to apply. `AppliedToInvoiceID` marks a credit consumed by an invoice exactly
once, so a retried check-out cannot double-claim it.

Until 020 is applied the legacy whole-row path still runs, latched by
`_RESERVATION_PAYMENTS_PARTIAL_SUPPORT` — and `_set_partial_credit_unsupported()` latches
**only** on a missing column or table, never on a deadlock, so a transient error cannot pin
the process to the legacy path.

`Invoices.PrepaidAmount` records the credit actually applied to an invoice.
`_build_invoice_insert()` omits the column when it does not exist yet
(`_invoices_have_prepaid_column()`), so **check-out keeps working before 018 is applied** —
do not hard-code the column back into the INSERT.

---

## 5. Loyalty is keyed on the CUSTOMER, not the room

Migration 019 re-keyed `LoyaltyAccounts` from `RoomNumber` to `CustomerID` (the PK).
`RoomNumber` survives as a nullable "last room seen" display column and is no longer a key
or unique. Three real defects came from the room key:

1. The next guest of a room inherited the previous guest's balance and tier, so they got a
   discount they never earned.
2. A returning guest's points fragmented across whichever rooms they happened to stay in,
   so Gold and Platinum were effectively unreachable.
3. Moving a guest to another room either stranded their points or handed them the target
   room's previous occupant's balance.

**Do not re-key any of this back to a room.**

### Storage layer

Customer-keyed, and the only layer that touches the tables: `get_points_by_customer()`,
`add_points_to_customer()`, `redeem_points_by_customer()`,
`get_lifetime_points_by_customer()`, `get_tier_details_by_customer()`,
`recompute_tier_by_customer()`, `ensure_customer_loyalty_account()`.

The old `*_by_room` names are kept as **thin delegates** that resolve the guest first via
`customer_id_for_stay(room_number, check_in=None)`, so the many call sites that naturally
talk about the stay being billed keep reading naturally while the balance follows the
person. Do not "simplify" the delegates away.

`customer_id_for_stay()` is the one bridge, and it returns `None` for a pre-019 or
front-desk stay. **`None` must read as zero points, no tier, no award** — never as an
error, and above all never as "credit the room". All of these log at debug and degrade, so
an unapplied 019 leaves check-out working with no loyalty.

### `Reservations.CustomerID`

Nullable FK, the link from a stay to its owner. Set by the booking desk, and by
`link_reservation_customer()` at check-in for front-desk and legacy stays. It is **cleared
to NULL on every re-let** — in `add_reservation()` and in `_book_reservation_in_conn()` —
because leaving the outgoing guest's link behind is precisely how the next guest inherits
their loyalty. `edit_reservation()` carries the moving guest's own `CustomerID` into the
target room's reused reservation row.

### Loyalty is not moved when a guest changes rooms

The pre-019 code re-pointed `LoyaltyAccounts` at the new room and, if the target room
already had an account, silently stranded the balance. Now there is nothing to move — the
account belongs to the guest. `LoyaltyTransactions.RoomNumber` is the room the points were
*earned* in and is **never rewritten**; `edit_reservation()` only updates the account's
`RoomNumber` pointer.

### Earning is per-night and paid-gated

On check-out, `check_out()` calls `award_stay_points()`:

```
points = nights x loyalty_points_per_night x room-category multiplier x tier multiplier
nights = stay_nights(CheckInDate, CheckOutDate)
```

- `stay_nights()` is the single source of truth for nights, shared with `post_room_charge()`
  so the room charge and the loyalty award can never disagree. It compares **calendar
  dates** (`.date()` when the value carries a time), not elapsed hours, and accepts `date`
  or `datetime` because `Reservations` stores `date` while callers parsing timestamps pass
  `datetime`.
- Category multipliers (`DEFAULT_ROOM_TYPE_MULTIPLIERS`, overridable via
  `loyalty_mult_<slug>`): Standard 1.0, Deluxe 1.5, Junior Suite 2.0, Suite 3.0,
  Grand Suite 4.0, Penthouse 6.0, Presidential Suite 8.0. Base `loyalty_points_per_night`
  is 100.
- **Points are paid-gated.** `award_stay_points()` only runs after
  `bill_room_transactions()` returns success, so a declined card awards nothing and the
  guest can retry. The same applies to orders, via `award_billed_order_points()`:
  `SUM(Amount)` of the billed transactions x `loyalty_accrual_points_per_unit` x tier
  multiplier, on a **pre-discount** basis — immediate when the guest pays now at order
  time, deferred to billing when the order is added to the room bill. Only F&B lines
  accrue; the nightly room charge is excluded because `award_stay_points()` already
  rewards the stay's nights, and counting both would pay the guest twice.
- `loyalty_accrual_points_per_unit` applies to **order/room-service spend only**, and is a
  **fraction** (0.5 pts/$), read through `_setting_float()`. Read as an int, 0.5 becomes 0
  and every order pays nothing — that is why the float is load-bearing, not cosmetic.
  Calibration (024): the room pays 100 points per night, so a $120 Standard night is
  **0.83 pts/$**; at the old 3 pts/$ a guest earned **3.6× more per dollar** ordering than
  for the room they slept in, and the category multipliers were meaningless because even a
  Deluxe could not catch up. The order rate must stay **below** the room rate —
  `points_per_dollar_order_vs_room()` is that comparison, it returns both figures for the
  settings screen, and `tests/test_billing_math.py` fails if the ordering inverts.
- **Points do not expire.** `loyalty_expiration_days` was editable on the settings screen
  and read by nothing; migration 025 deletes it. Recency is rewarded through tier promotion
  instead — see `docs/DEVIATIONS.md` §4.
- Tier thresholds (migration 010): Bronze 0 / Silver 1000 / Gold 2500 / Platinum 7500.
  `DEFAULT_TIERS` matches. The `UPDATE`s are guarded so they only fire while a tier is
  still at its old default.

**Awards must be idempotent.** `award_stay_points` guards on
`SourceID = 'stay:{customer_id}:{room}:{check_in}'` and `award_billed_order_points` on
`'order_pay:{customer_id}:{room}:{sorted tx ids}'` in `LoyaltyTransactions`. The **customer
id is load-bearing** in both: a room is re-let, so two different guests can occupy the same
room on the same dates, and a room-only key would reject the second guest's award as a
duplicate of the first's.

### Panels

`view_my_loyalty_status()` gates on `validate_room()` like every other guest-facing
feature. It used to ask only for a room number, which both disclosed a balance to anyone who
knew a room and — now that balances are person-keyed — would have shown the wrong guest's
points.

The admin loyalty panel identifies the guest by name/email via `_pick_loyalty_customer()`.
It deliberately will **not** act on a surname alone, so a clerk cannot adjust the wrong
person's balance.

### `reports.py` loyalty export

Joins `LoyaltyAccounts` to `LoyaltyTransactions` on `CustomerID` and takes `--customer`
(id, email, or `LastName FirstName`) in addition to `--room`. Joining on `RoomNumber` paired
each account with *every* transaction ever taken in that room, so a statement listed other
guests' history. A `--room` filter now resolves the guest on that stay and reports that
person's whole history; an ambiguous name/email **raises** rather than exporting the wrong
guest. This report has **no** pre-019 fallback and will error until 019 lands.

### Legacy balances are deleted, not migrated

Migration 019 adopts a room's balance onto the current occupant where one is identifiable,
and DELETEs any row still without a customer. A balance attributed to a room cannot be
attributed to a person, and guessing an owner would credit the wrong guest with someone
else's history. The migration `SELECT`s the resulting counts so the operator sees what was
dropped.

---

## 6. What has and has not been run

Which of these have actually been run changes over time, so this section is kept to the
short version. The unit tests are database-free by design (the money rules are pure
functions, the storage layer is mocked), so they can never cover any of it.

**`hotelSystem` had its first live booking run on 5 October 2026** — one unassisted
check-in → F&B order → check-out-with-credit → loyalty stay, covering the settlement
path 019–022 back. What it did and did not cover, and which invariants were observed,
is in [LIVE-RUN-2026-10-05.md](LIVE-RUN-2026-10-05.md); the gap that remains is the
cancel/refund, declined-card, and interrupted check-out paths.
`tests/verify_e2e.py` does not change that gap — it builds a disposable database,
exercises the code there and drops it.

What `verify_e2e.py` now covers, on every run, against a server that really is SQL Server:

- The booking-desk money helpers — `booking_quote`, `booking_payment_options`,
  `refund_decision`, `allocate_booking_credit`, `settle_with_prepayment`, `stay_nights`.
- Both loyalty award paths, **including that a repeat pays nothing**. That idempotency claim
  used to rest on reading the code.
- 019's guest identity: `customer_id_for_stay()`.
- 021's booking-reference collision: `record_booking_payment()` raising `BookingRefTaken`, on
  the `conn=` path both production callers use.
- 022's captured-rate read, against a row that genuinely carries a captured rate.
- **The loyalty report**, at all three of its scopes — unscoped, by customer, and by room —
  read back out of the CSV it actually writes rather than out of a return value.
- **Settlement resumability**: a failure injected at each of `check_out()`'s five steps in
  turn, followed by an ordinary re-run, must reach exactly the state an uninterrupted
  check-out reaches.

The room-scoped one is the interesting check, because it is the whole content of 019: a
statement asked about one room must report that **guest's** history, including points earned
in rooms that were not asked about. A query that filtered the ledger on the room would still
produce a plausible-looking CSV naming the right guest, so the harness places a ledger row in
a second room first — without that, "whole history" and "this one stay" are the same rows and
the test passes either way. The seed gave guest A every row in a single room, which is why this
had to be arranged rather than read off the fixtures.

### Settlement is resumable, not atomic — and the difference is deliberate

`check_out()` is five steps in sequence, each committing on its own connection. The obvious
remedy is to wrap them in one transaction, and that is the wrong remedy here for a reason
recorded in the code: `bill_room_transactions()` prompts for point redemption at
`main.py:5713`, and the comment at `main.py:6139-6142` says the room charge posts **first**
precisely so that a declined card can be retried without re-posting it. A transaction
spanning a console prompt would hold locks across human think-time and destroy that retry. So
the property worth having is not atomicity, it is **resumability**: an interrupted settlement
re-run must converge on the same state as one that was never interrupted. That is what
`verify_e2e.py` Phase 5b asserts, and all five steps already satisfy it.

The harness needed two guards to make that non-vacuous, and both are load-bearing. The
injector sits *outside* each helper rather than inside it, because five of the six settlement
helpers have their own `except Exception` and would otherwise swallow the simulated failure
and report the step as successful. And each scenario asserts both that the injection fired
and that the interrupted state **differs** from the finished one — otherwise a step that had
quietly stopped being called, or an interruption that changed nothing, would report a pass.
The second guard is not theoretical: on its first run the prompt router reported a prompt it
did not recognise (`Do you have a discount code?`) instead of silently mis-answering it.

The assertion was proven able to fail before being trusted, by restoring the exact
`post_room_charge()` guard mismatch described below. It went red on four of the five
scenarios, reporting `room charges=2`, `invoices=2`, `invoiced total=502.85` where an
uninterrupted run gives `276.85`.

Point redemption is deliberately **not** covered here, though Phase 5c covers it separately.
Answering "no" keeps the resumability scenarios about one property; mixing redemption in
would fold two problems into a single result, and a failure would not say which one broke.

The one thing it still does **not** touch is **the live database**, for the reason above.

Migrations **022-025** were verified by `tests/check_applied_migrations.py`, which confirmed
the `Reservations.NightlyRate` shape, the seeded `business_date`, the recalibrated accrual,
the deleted expiry row, and both date reports running against the live server.

That live database had **no reservations in it**, which at the time meant two things were
unproven. Both are now proven on the disposable database, and neither is proven on the live
one:

- The 022 backfill statement was run against rows that genuinely lacked a rate, rather than
  finding nothing to update. Precisely: migration 022's own file runs in Phase 2, before the
  seed exists, so it is `verify_e2e.py` that re-runs 022's `UPDATE` in Phase 5 — the same
  statement, not the migration.
- The captured-rate read path returned a real captured value rather than taking the fallback
  branch — `get_captured_nightly_rate()` and `stay_nightly_rate()` both assert it.

Both claims used to be stated here as *never verified at all*, which was true when written and
stopped being true the moment `verify_e2e.py` ran.

`tests/seed_smoke_test.sql` populates a stay for exactly this, and
`tests/seed_smoke_test_cleanup.sql` undoes it. Both are yours to run. The agent may not run
either against the live database (AGENTS.md §4) — but a scratch database is not the live
database, and `tests/verify_e2e.py` is meant to be where this gets exercised repeatably.

### 2 October 2026 — `tests/verify_e2e.py` now exists, and two bugs came out of it

`python tests/verify_e2e.py` builds a disposable database, runs the schema through both
install paths, exercises the booking code against it and drops it. See AGENTS.md §4 for the
rules that authorise it and §6 for how to run it.

**Fixed: `post_room_charge()` double-charged the room on any retry.** The once-only guard
selected on `Transactions.Description = f"Room charge for {check_in}"`, but the `INSERT`
wrote `f"{marker} - {room_type}"`. Those are never equal, so the guard could not match its own
row: calling it twice against a real server produced two room-charge rows for one stay
(`ItemID IS NULL AND ChargeGroup = 'Room'`). The docstring's idempotency claim was therefore
false.

This mattered more than an ordinary double-post, because the room charge is the line a guest
argues about, and the retry paths are ordinary: a declined card, a re-entered card, a
re-run of check-out after a crash. The loyalty award beside it was always idempotent
(verified — a repeat `award_stay_points()` pays nothing), so before the fix a single retry
left the folio charging twice while the points ledger paid once. The description is now
composed once and used by both the guard and the `INSERT`, so the two cannot drift apart
again, and `verify_e2e.py` asserts it.

**Fixed 3 October 2026: `main.get_connection()` was a duplicate that swallowed every database error.** It caught, logs, and re-yielded — the exact shape AGENTS.md §5 forbids, in a copy at `main.py` rather than the correct one in `db.py`. Because a generator cannot yield after a `throw`, every error raised inside a `with get_connection()` block reached the caller as `RuntimeError: generator didn't stop after throw()`. `main.get_connection` is now a plain alias for `db.get_connection`, so there is one definition and it is the correct one. A failing statement now escapes as `pyodbc.ProgrammingError` carrying the server's own message; a failed *connect* still yields `None`, which is what the `conn is None` guards exist for.

**This never affected the booking desk, and an earlier version of this section said it did.** Both callers pass `conn=` — `_write_booking_charge()` and `cancel_booking()` — which takes the branch that opens no connection of its own and never reached the broken copy. The retry loop described in §2 has always worked, and `verify_e2e.py` asserts it against a real server. The earlier claim came from an assertion that called `record_booking_payment()` *without* `conn`, a path no code in `main.py` takes; that file contradicted itself on the point, and §2 was right.

Two things did **not** get fixed by this, and both were mispriced while it was open:

- The 130 `conn is None` guards are live and correct — `create_connection()` returns `None` when a **connect** fails, and that is the only path to them. They are not there for query failures and should not be removed.
- Rollback safety has now been **audited**, and the standing worry was overstated. There are
  78 `conn.commit()` sites but only **5** functions commit in more than one
  `with get_connection()` block, and all five are correct by design — three are menu loops
  whose commits sit in mutually exclusive branches, two commit a primary effect and then a
  best-effort notification inside its own `try/except`. A single block whose executes all
  precede one commit was already atomic. There were also already 5 explicit rollbacks in the
  refund retry loop, so "nothing rolls back on error" was wrong. What the audit *did* find
  missing was that the guarantee was inherited from the driver and untested;
  `db.get_connection()` now rolls back explicitly and Phase 5d asserts it. `main()`'s menu
  loop still must not grow a top-level exception handler — but the reason is
  inter-procedure sequencing, which remains unaudited. See AGENTS.md §7.

Neither bug was visible to `check_schema_sync.py`, `check_migration_sql.py`,
`check_applied_migrations.py` or the unit suite, because all four work on text or on a
catalog, and neither had ever had the code executed.

Still unexercised, and not claimed otherwise: the interactive booking wizard and check-out
screens (the harness calls the functions they delegate to, not the `rich` flows), and any
concurrency behaviour.

### A failed check-out now says what is left to do

Phase 5e injects a failure at each of `check_out()`'s five steps and measures the residue,
because until now nothing reported it. `check_out()` returned `None` on success and on
failure alike and logged only the exception, so a clerk had to infer whether the guest was
still in the room, still holding a working key card, and still owed money.

`settlement_outstanding()` derives that from the rows rather than from memory — the failed
process is gone, so there is nothing in memory to ask — and `announce_settlement_outstanding()`
prints it on both failure paths. The sharpest residue is a failure at
`revoke_active_key_cards()`, which leaves a guest who has already paid holding a working key
card to a room the system still shows as occupied.

An advisory review proposed reordering the steps so key revocation and room status happen
*before* payment. **That was not applied**, and the reason is worth keeping: it trades this
residue for a worse one. Revoking first means a declined card leaves a guest who has not paid,
is still in the room, and cannot open their own door. No ordering of five non-atomic steps
leaves nothing behind, so the choice is which residue you would rather be *told* about.
See [DEVIATIONS.md §10](DEVIATIONS.md).

Phase 5e asserts the negative direction too, and that half matters more: a **completed**
check-out reports nothing outstanding. A signal that fires on healthy rooms teaches staff to
ignore it.

### A redemption cannot outlive the payment it was given for

The declined-card path has its own phase (Phase 5c) and it exists because running it once
produced a **finding**, not a pass. Redemption used to commit before the card prompt, so a
declined card cost the guest their points with nothing billed in exchange — and because that
was the only loyalty mutation in the app with no `SourceID`, nothing in the ledger identified
the loss. Measured, before the fix: `points before=2600 after=2100 | redemption ledger rows=1
| invoices=0`.

Redemption now records only the *intent* at the prompt; the deduction happens in the same
transaction that inserts the invoice, alongside `apply_booking_credit()`, which was already
doing exactly this for the booking credit. Phase 5c now asserts all three outcomes — a
declined card leaves the balance and the ledger untouched, a settled redemption deducts
exactly once with a `SourceID` and an agreeing invoice, and a repeat run takes nothing
further. Those assertions were proven red against the old code, which reported
`2600 -> 2100, with nothing billed`. See [DEVIATIONS.md §9](DEVIATIONS.md).
