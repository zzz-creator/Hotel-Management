# Test plan — booking desk, loyalty and check-out, driven through the app

Everything here is done **through the app UI**. No SQL. Run it in this order; later phases
depend on earlier ones.

Migrations 001-021 are applied but have **never been exercised against a live database** —
only the schema has been. These phases are what closes that gap.

## Before you start

| | |
|---|---|
| Card that passes | `4111111111111111` · expiry `12/2030` · CVV `123` |
| Card that must fail | `4111111111111112` (fails Luhn by one digit) |
| Tax rate | 13% |
| Refund cutoff | 7 days before arrival |
| Loyalty per night | 100 × room multiplier × tier multiplier |
| Tiers | Bronze 0 (0%) · Silver 1000 (5%) · Gold 2500 (10%) · Platinum 7500 (15%) |
| Rates | Standard 120 · Deluxe 180 · Junior Suite 260 · Suite 400 · Grand Suite 650 |
| Discount codes | literally `1`=50% `2`=30% `3`=20% `4`=100% `5`=60% `6`=90% |
| Rooms to use | `12045` (Standard) and `12700` (Deluxe) — verified free |

A room is reusable once its stay is in the past: check-out leaves the reservation row, and a
completed past stay is re-booked for the next guest. You do not need a fresh room each run.

---

## Phase 1 — Public booking desk

Main menu → **3. Bookings**. No login at the main menu; the desk is public.

### 1.1 First-visit registration *(019)*
**Do:** 1. Book a Room → at "Email address" enter an address you have never used
(e.g. `dana.new@example.test`).
**Expect:** it asks for last name, first name, then a password, and confirms
"Your booking account is ready." It does **not** turn you away.
*Why:* a guest with no account can't book, so refusing would make them unrecognisable on a
return visit.

### 1.2 Date validation
**Do:** try each of these, expect a refusal each time and no reservation:
- check-in in the past → "Check-in date cannot be in the past."
- check-out before check-in → "Check-out date must be after check-in date."
- check-out equal to check-in → "That stay is at least one night long."

### 1.3 The quote *(pure money rules)*
**Do:** pick **Deluxe**, check-in **10 days out**, check-out **2 nights later**.
**Expect** a "Booking Estimate" box:
- Nightly rate $180.00 · Nights 2
- Room subtotal $360.00 · Tax (13%) $46.80 · Estimated total **$406.80**
- An explicit line saying it is an estimate and the final bill is priced at check-out.

### 1.4 Refund policy comes *before* the money *(the important one)*
**Expect:** a **"Cancellation Policy" box is shown before** the payment options, naming the
free-cancellation deadline, what happens after it, and that from check-in onwards you must
contact the front desk. Read the date it states.
*Why:* a guest shown the policy only on the confirmation screen has already handed over a card.

### 1.5 Payment options
**Expect exactly two** options — a one-night deposit, and pay in full. There must be **no**
"pay at check-out" option, and the list must never be empty.
**Expect** deposit = $203.40 (one night $180 + 13% tax) for a 2-night stay.

### 1.6 Complete the booking
**Do:** choose pay-in-full, then `4111111111111111` / `12/2030` / `123`.
**Expect** a "Booking Confirmed" box with: booking reference (`BK-` + 6 hex), room number,
guest name, both dates, estimated total, amount paid now, estimated balance at check-out,
"Card ending 1111", and the cancellation policy **repeated with the exact amount at risk**.
**Write the reference down** — you need it for 1.8.
*Note:* the guest is never asked to pick a room; the app assigns the first available room of
the chosen type and that number *is* the reference.

### 1.7 Declined card holds no room *(the money-safety test)*
**Do:** start another booking, same room type and dates, and at the card prompt enter
`4111111111111112`.
**Expect:** "Invalid credit card number. Payment Cancelled." then
"Payment was declined, so the room has not been held. Nothing was charged."
**Then:** 2. View My Booking → the *first* booking is still there and the room was not
consumed by the failed attempt.
*Why:* payment is taken before the commit and the whole booking rolls back, so a declined
card must never leave a held room or a phantom reservation.

### 1.8 View, then cancel
**Do:** 2. View My Booking → enter the reference from 1.6.
**Expect:** the stay, with the room, dates and amount paid.

**Do:** 3. Cancel My Booking → enter the reference, confirm, then type the reference back
exactly.
**Expect:** "full refund of $406.80 to the original card" (arrival is 10 days out, past the
7-day cutoff), and "the name of the room was released".
**Then:** 2. View My Booking again → no booking.
**Important:** the money rows are **kept** after cancellation — that is deliberate, it is the
only surviving history and it is what stops the reference being reissued to someone else.

### 1.9 Forfeit, and the arrival-day refusal
**Do:** book again with check-in **3 days out** (inside the 7-day cutoff), then cancel.
**Expect:** the paid amount is **forfeited**, not refunded, with the reason stated.

**Do:** book again with check-in **today**, then cancel.
**Expect:** cancellation is **refused** and you are told to contact the front desk.
*Why:* cancellation DELETEs the reservation, so allowing it on the arrival day would wipe an
in-house stay, free the room on the housekeeping board while the guest is in it, and leave
check-out with nothing to bill.

---

## Phase 2 — Ownership and disclosure

### 2.1 A booking is readable only by its owner *(019)*
**Do:** sign in as the guest from 1.1, then use 2. View My Booking with **a different
guest's reference** (book a second guest in a second terminal, or reuse any reference you
have).
**Expect:** "not found" — and the wording must **not** distinguish "no such code" from
"someone else's code".
*Why:* a reference is 8 characters and brute-forceable. Without the owner check, a guess
would disclose another guest's room and let you cancel their stay, which destroys the booking
*and* pays out their deposit to you.

### 2.2 Identity check discloses nothing *(front desk)*
Main menu → **1. Customer** → **2. Place Order**. At the identity prompt, give a real last
name with a **wrong first name**.
**Expect:** "We could not find a reservation for those details." — and **no list of other
guests' room numbers or first names**, even though the surname is real.
*Why:* a last name is not unique. The old code listed every match, which handed a stranger's
room number and first name to anyone who knew a surname.

### 2.3 Room-number normalisation
**Do:** at the same prompt enter the room with a leading zero, e.g. `010045` for room `10045`.
**Expect:** it is accepted and normalised.

---

## Phase 3 — Front desk: reservation, check-in, and the loyalty link

Admin → **2. Admin** (log in as `root` or `master`) → **1. Add Reservation**.

### 3.1 Create a front-desk stay
**Do:** room `12045`, last `Rivera`, first `Maya`, check-in **today**, check-out **+2 nights**.
**Expect:** created. Room status becomes `Occupied` only at check-in, not here.

### 3.2 Check in
Main menu → **1. Customer** → **1. Check In** → `Rivera` / `Maya` / `12045`.
**Expect:** room status `Occupied`; it asks for optional email and phone.
**Do:** enter the **same email you registered in 1.1**.
**Expect:** "Contact details saved to your customer profile."
*Why this is the key step:* a front-desk stay has no online booking, so this is what links the
stay to the person. Their loyalty balance follows **them**, not the room.

### 3.3 Key card issued
**Do:** Customer → **14. My Key Card** → `Rivera` / `Maya` / `12045`.
**Expect:** an active card number. Admin → **31. Door Access Control** → test that card at
the reader: it should **grant**, and log an event.

---

## Phase 4 — In-house guest features

All of these gate on the same identity check (`Rivera` / `Maya` / `12045`).

| Menu | Action | Expect |
|---|---|---|
| **2. Place Order** | order a few items, choose **1 = Pay now** | card prompt, then an **order number** |
| **2. Place Order** | order again, choose **2 = Add to room bill** | no card prompt, another order number |
| **8. Track Order Status** | enter the first order number | shows `Placed` and its items |
| **11. View My Loyalty Tier & Perks** | — | tier, points, perks |
| **15. My Notifications** | — | any message for the room |
| **9. Contact Concierge** | submit a request, then **10. View My Concierge Requests** | request is listed, not lost |
| **5. Provide Feedback** | submit a rating | accepted |

**Expect** the pay-now order to also award loyalty points immediately, and the room-bill
order to award them at check-out instead — not both, and not neither.

Admin → **9. Order Management** → advance the first order. Re-check
**8. Track Order Status**: the state and the guest's notification should both move.

---

## Phase 5 — Check-out, the money path

Customer → **3. Check Out** → last name + room number.

### 5.1 The split folio
**Expect** a receipt with **two groups**:
- **Room** — at face value, no discount applied to it, plus its own tax.
- **Food & Beverage** — the orders from 4.1, with any discount and tax.

Discounts apply to the **F&B group only**; the room is charged at face value. One combined
table would wrongly imply the discount hit the room.
**Expect** an offer to redeem points. Redeem some and check the arithmetic: 100 points = $1.00,
credit can never exceed the bill, and a confirmed $0 bill is handled without error.

### 5.2 The room charge is posted here, not at check-in
**Expect** exactly one room charge line for the stay, and that re-running check-out after a
*failed* payment does **not** add a second one.
**How to test the retry:** run check-out and deliberately fail the card
(`4111111111111112`). Expect "Payment declined. Please settle the bill before completing
check-out." Confirm the room is **still `Occupied`** (not flipped to `Dirty`) and no invoice
was written. Then check out again with the good card and confirm only one room charge.

### 5.3 After successful check-out
**Expect:** room status `Dirty`; the key card is **revoked** (retest at the reader → denied,
and the denial is logged); an invoice exists; loyalty points were awarded for the stay.

Customer → **12. Print My Invoice**.
**Expect** an itemised invoice with a `exports/invoice_<id>.txt` copy, where the pay-now line
is tagged differently from the check-out line ("card (pay now)" vs "card (check-out)").

### 5.4 Loyalty followed the person
Customer → **11. View My Loyalty Tier & Perks** again.
**Expect** the balance went **up** by `nights × 100 × room multiplier × tier multiplier` for
a Standard room, and the tier was recomputed.

---

## Phase 6 — The partial-credit case *(most important; this is what 020 fixes)*

This is the scenario that motivated the migration, and it is the one most likely to be
wrong. Do it deliberately.

### 6.1 Overpay, then have the rate cut
1. Book a **Deluxe** stay, check-in 10 days out, **2 nights**, and **pay in full** ($406.80).
   Note the reference.
2. Admin → **25. Manage Pricing & Settings** → Room Types & Nightly Rates → set
   **Deluxe to 50.00**.
3. Customer → **3. Check Out** for that room, no discount code, no points redeemed, pay with
   the good card.

**Expect:**
- The bill is now **113.00** (2 × $50 room + 13% tax), *not* 406.80.
- The prepayment is credited only up to the bill. The invoice's **prepaid amount is 113.00**,
  and **amount paid is 0.00**.
- The app reports the remainder as **unused credit** — it must not be silently swallowed, and
  the bill must not go negative.

### 6.2 Read the invoice
Admin → **29. Invoices & Printing** → print that invoice.
**Expect:** `PrepaidAmount` = 113.00.
**Before the 020 fix this read 406.80** — a $293.80 credit that no longer existed was
recorded as spent, and the invoice and the ledger disagreed.

### 6.3 Restore the rate — do not skip this
Admin → **25. Manage Pricing & Settings** → set **Deluxe back to 180.00**.
Confirm before moving on; a left-lowered rate will quietly change every later booking total.

---

## Phase 7 — Reports and the audit trail

- `python reports.py --report loyalty --customer "Rivera"` — the guest's whole history,
  regardless of which rooms they stayed in. Also try `--customer` with their email, and with
  just `12045` (a room resolves to the guest who held that stay).
- Admin → **27. Export Reports** → export invoices and revenue.
- Admin → **30. Customer Profiles** → search `Rivera`: profile, stays and invoices listed
  separately, with a stay never duplicated just because the room has several invoices.
- `python reports.py --report audit` — expect `CREATE`, `LOGIN` and `CANCEL` entries for what
  you just did.

---

## Failure signatures — what should *not* happen

| Symptom | Means |
|---|---|
| A declined card still shows the room as booked | payment is not inside the transaction |
| Check-out adds a second room charge | the idempotency marker on `Description` is broken |
| Room flips to `Dirty` after a declined card | status is set before payment succeeds |
| Loyalty balance changes when a room is re-let | a balance is still keyed on the room |
| Guest A sees Guest B's booking | the owner check on `_find_booking` is missing |
| "No such booking" vs "not your booking" differ | the lookup is confirming a guess |
| Invoice pre-paid amount exceeds the bill | partial credit is being recorded whole-row |
| A booking total ignores a rate change | the rate is not snapshotted at check-out |
| Refund names a card the guest never used | it read the last card on file, not their own |
| `View My Booking` finds nothing for a real booking | `StayCheckIn` and `CheckInDate` disagree |

---

## What the UI cannot prove

Be honest about these three; they need a database read, and I can run any of them read-only
on request:

1. **021's uniqueness under a real race.** Two guests cannot be forced into the same
   8-character reference through one UI session. The index is the guarantee; the retry path
   only executes if two sessions collide. Two terminals booking simultaneously is the closest
   approximation.
2. **020's partial arithmetic.** Phase 6 shows you the invoice's pre-paid amount, which is the
   user-visible consequence. Confirming the ledger row itself carries
   `AppliedAmount = 113.00`, `IsApplied = 0` and a non-zero outstanding balance needs a query.
3. **019's legacy backfill.** Every table is empty, so the adoption window, the duplicate
   collapse and the orphan delete all ran as no-ops. They only do real work on a database with
   history, and they are the part of 019 with no live evidence behind it.

## Cleanup

- Cancel any remaining bookings through **3. Bookings → 3. Cancel My Booking** (this keeps the
  money rows by design).
- Admin → **2. Delete Reservation** for front-desk stays. Note a room with billing history
  refuses deletion and tells you to re-book instead — that guard is intentional.
- Admin → **32. Delete All Reservations** exists for a full reset, behind a master override
  and a typed confirmation. It also resets `Rooms.Status`. Use it only if you want the board
  clean.
