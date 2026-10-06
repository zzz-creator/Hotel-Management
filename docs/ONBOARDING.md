# Staff onboarding

What has to happen, in order, between "the database exists" and "the front desk can take a
booking that a guest can pay for". Written for whoever is handed the system, not for
whoever wrote it.

Read this after `database.sql` has been applied and before anyone tries to log in.

> **Most of this runbook is now automated.** The first time you start the app against a
> database that has never been set up, a **first-run wizard** runs before the main menu and
> walks you through it: it creates your first administrator, and offers starter items,
> and rooms. Every step after the login is optional and skippable.
>
> **Admin Panel → 34. Setup Checklist** shows what is still outstanding at any time, and can
> re-run the setup actions. It lives in the Admin Panel rather than the main menu on
> purpose: the main menu is the guest-facing surface, and this screen can write accounts.
>
> What the wizard cannot do is listed under **What is still manual** below.

---

## 1. What a fresh install gives you, and what it does not

`database.sql` is the authoritative fresh-install script and it seeds exactly **three**
tables. Everything else is either empty or created on demand.

| Table | After `database.sql` | Who fills it |
|---|---|---|
| `HotelSettings` | 15 rows | seeded, then Admin → Pricing & Settings |
| `LoyaltyTiers` | 4 rows (Bronze/Silver/Gold/Platinum) | seeded; Admin → Loyalty Management |
| `RoomTypes` | 7 rows | seeded, **and** re-checked at every start by `ensure_room_types_seeded()` |
| `Users` | **empty** | you, by hand — see step 2 |
| `Rooms` | **empty** | created as a side effect of booking, or seeded by migration 008 |
| `Items` | **empty** | you, in the Admin Panel |
| `Amenities`, `Promotions` | **empty** | migration 015, or Admin → Manage Amenities / Promotions |
| `Discounts` | **empty** | you, in the Admin Panel |
| `Reservations`, `Transactions`, `Invoices` | **empty** | normal use |

Two of those matter more than they look:

- **No rooms.** The room dashboard, housekeeping report and availability search all read
  `Rooms`, so they are empty until a room exists. Rooms are created by
  `upsert_room_if_missing()`, which the booking, check-in and edit paths call for you — so
  the *first* booking invents its own room. For a hotel that is meant to already exist,
  apply `migrations/008_rooms_seed.sql` instead (it seeds a 150-floor tower, 138,180 rooms).
- **No items.** Nothing can be ordered and no folio can be split until at least one item
  exists, because the room charge is written as a `Transactions` row pointing at an
  `ItemID`. This is the step people skip.

---

## 2. Create the first login — the wizard does this

`Users` is seeded by nothing: not by `database.sql`, not by any migration. And the only
in-app way to add a user, `add_user()`, lives *inside* the Admin Panel, which needs a
login to reach. So the first account used to have to be inserted directly:

```sql
USE hotelSystem;
INSERT INTO dbo.Users (Username, Password, Role) VALUES (N'admin', N'admin', N'admin');
```

**You no longer need that.** Start the app on an un-set-up database and the wizard asks for
an administrator username and password and creates it (`create_first_user()`). It runs once,
before the main menu, because a database with no accounts has nothing to log into.

Roles are `admin`, `staff`, `manager` and `valet`, and they are not equal: the `manager`
Admin Panel has no Items, Users or Pricing sections, and `staff` gets only reservations and
guest services. Give people the narrowest role that lets them do their job. The wizard also
offers to create a `master` account — see the note below for why that one matters.

> Passwords are stored and compared in plaintext here, on purpose — this is a teaching
> project (see AGENTS.md §3, DEVIATIONS.md §8). Change the bootstrap password anyway.

`require_master_override()` falls back to an account literally named `master` when
`config.ini` has no `[hotel] master_secret`, so if you want that fallback to work, create
that account too. The wizard asks. `config.ini.example` ships with a **blank**
`master_secret`, so on a fresh checkout the `master` account is the only thing that makes
the override work at all — which is why **Setup Checklist** flags it.

The `master` account is created with the **admin** role, not a role of its own. The
override matches on the username alone, but the Admin Panel only branches on
`staff` / `manager` / `admin` / `valet` / `it`, and **Add User** only offers `a` / `s` / `m`.
A row with role `master` would be an account that exists and cannot be signed in to.

Once you are logged in as `admin`, use **Admin Panel → 19. Add User** for everyone else.

> The wizard runs unauthenticated. That is unavoidable: there is no account to authenticate
> *against* yet, so it assumes what the database already assumes — that whoever is at the
> console can also open SSMS against the empty database. Two guards keep it from becoming a
> hole. It never runs again once `onboarding_complete` is written, and
> `create_first_user()` refuses outright if **any** account already exists, so it cannot be
> used to add an admin to a live system. If you inherited a database that already had
> hand-inserted accounts, the wizard says so and skips the step rather than looping.

---

## 3. Add items (the wizard offers this)

The wizard step, and **Setup Checklist → 3**, both open `_offer_item_catalogue()`. It shows a
starter catalogue of six items and then lets you add as many of your own as you like, in a
loop, until you answer "done". Take the starter set, your own, or both — nothing here is
mandatory except having *something*, because nothing can be ordered until one sellable item
exists.

- **Starter catalogue** — `seed_default_items()`. Reads `MAX(ItemID)` and continues from
  there, and skips anything already present by name, so it is safe against a hotel that has
  hand-added items.
- **Your own item** — `add_custom_item()`. Asks for a name, a price, and a pricing rule.

**You do not add a room charge.** The app posts the room charge itself at check-out as a
`Transactions` row with `ItemID = NULL` and `ChargeGroup = 'Room'`, and it is guarded against
posting twice. There is deliberately no "Room Charge" item in the starter catalogue: because
`record_transaction_for_room()` defaults `ChargeGroup` to `'F&B'`, an orderable "Room Charge"
item would post a second charge *on top of* the automatic one and accrue loyalty points on
it. Every row in `Items` is a genuine F&B line.

`add_custom_item()` assigns `ItemID` as `MAX(ItemID)+1` rather than asking, because
`ItemID` is a plain `int` primary key, not an identity column — a typed id that collides with
a row that already exists gives the operator a constraint violation instead of an item.

**Admin Panel → 13. Add Item** (`add_item()`) is the older path and is still there. It *does*
ask for the `ItemID`, so use it when you want a deliberate numbering scheme (by category, say)
rather than a contiguous one. It does not validate the id or the price, so prefer
`add_custom_item()` unless you specifically need to choose the number.

An item needs a name, a price, and a `PricingRule`. The charge group is not set here: it is
written on the `Transactions` row when the charge is billed, `'Room'` for the room charge
and `'F&B'` for anything ordered (`ChargeGroup`). Getting this wrong double-counts loyalty
points, which is why `award_billed_order_points()` filters on `'F&B'` only.

**Admin Panel → 16. View Items** to confirm.

---

## 4. Set prices

**Admin Panel → 25. Manage Pricing & Settings → 6. View / Edit Room Types & Nightly
Rates** (`update_room_type_rate()`).

A `RoomTypes` row is a **price list**. A confirmed booking is a **contract**: the nightly
rate is captured onto the reservation when the stay is made, so editing a rate later does
not re-price a stay that is already booked. A reservation with a NULL `NightlyRate` is one
made before rate capture existed, and check-out falls back to the current rate and says so.

The same menu sets the tax rate, the peak/off-peak factors and the booking cancellation
window. The loyalty points rates are calibrated, not free-form: if you change the order
accrual, check the ordering still holds (points earned per dollar on F&B must stay **below**
points earned per dollar on the room). The screen shows both rates side by side.

---

## 5. Rooms (the wizard offers this)

Either let them appear as bookings are made (see step 1), or use the wizard / **Setup
Checklist → 4. Seed rooms**, or apply `migrations/008_rooms_seed.sql` for the full tower. To
check what you have: **Admin Panel → 29. Rooms & Housekeeping → 1. Room Dashboard**. Status
changes are **→ 3. Update Room Status**.

A room number is `<floor><3-digit code>` (floor 9, code 012 = `9012`) and the column is
free text — there is no foreign key from a stay to a room, deliberately.

The room seeder (`seed_rooms()`) offers three layouts:

| Layout | What it does |
|---|---|
| 150-floor tower | Exactly `migrations/008_rooms_seed.sql` — 138,180 rooms in three tiers. Large insert; you are asked to confirm. |
| 20 × 40 | A small rectangular hotel. |
| Custom | Floors × rooms per floor, with the category derived from how far up the building the floor sits. |

Two things worth knowing:

- **It never overwrites.** The insert is guarded by `NOT EXISTS`, so a room that already has a
  guest in it keeps its category and housekeeping status, and you can re-run it to top up a
  partial seed.
- **The bounds are the app's, not a round number.** Floors cap at **150** because
  `view_rooms()` rejects anything outside 1-150, and rooms per floor cap at **999** because
  the code is three digits and both `view_rooms()` and the housekeeping report recover the
  floor as `LEFT(RoomNumber, LEN(RoomNumber) - 3)`. Seeding past either would create rooms
  the UI cannot reach.

---

## 6. Optional reference data

Only needed if you skipped the migrations. All of it is editable afterwards.

- **Amenities** — Admin Panel → 17. Manage Amenities (`manage_amenities_menu()`)
- **Promotions** — Admin Panel → 18. Manage Promotions (`manage_promotions_menu()`)
- **Discount codes** — Admin Panel → 24. Manage Discount Codes (`manage_discount_codes()`)

---

## 7. Key cards

Cards are issued at check-in and expire at the reservation's check-out date; a card opens a
door only while it is `Active` and unexpired. Issuing, revoking, reporting lost and testing
a card at the reader are all in **Admin Panel → 32. Door Access Control**
(`door_access_menu()`). Nothing to set up first — a card is created for you when a guest
checks in.

---

## 8. The business date

As of 5 October 2026 there is nothing to set: the business date IS today's date, and
`close_day` / the stored `HotelSettings['business_date']` clock were removed. (If your
run of this guide predates that change, the stored clock and the Admin Panel
**Business Date** entry are gone; the migrations 001-025 already applied still carry the
row, which the app now ignores.)

A report for a day that has closed still works -- occupancy and the housekeeping board
take an explicit date or window (`on_date`, `start_date`/`end_date`); pass the date, the
clock does not need to be rewound.

## 9. Smoke test

1. **Bookings → book a stay**, two nights, in a room number of your choosing.
2. **Admin Panel → 29. Rooms & Housekeeping → 1.** The room should appear and be `Available`.
3. **Order something** through the guest menu — this exercises the item you added in step 3.
4. **Check the guest out.** The folio must split into "Room Charges" and "F&B", with tax on
   each, which only works if `ChargeGroup` and the `Invoices` split columns exist.
5. **Admin Panel → 30. Invoices & Printing** — print it and sanity-check the arithmetic.
6. **Admin Panel → 28. Export Reports → occupancy and revenue** for today. Occupancy is
   derived live from `Reservations`, not accumulated.

If step 4 produces a single undifferentiated total, the split-folio columns are missing —
that means the database was built from an out-of-date `database.sql`.

---

## Where the rules live

This page is the runbook. The rules it refers to are documented elsewhere and are not
reproduced here on purpose:

- **Tables, columns and what each migration added** — `docs/SCHEMA.md`
- **Booking money, deposits, refunds, loyalty** — `docs/BOOKING.md`
- **Things this app deliberately does not do, and why** — `docs/DEVIATIONS.md`
- **Schema rules for agents and developers** — `AGENTS.md`

## What is still manual

Everything the wizard cannot reach, in order:

1. **Applying `database.sql`**, and creating the database and the SQL Server login. The app
   never issues DDL for these — schema ships as `.sql` files you run yourself (AGENTS.md §4).
2. **`config.ini`.** It is untracked and holds the SQL Server password and the
   `[hotel] master_secret`. Copy `config.ini.example` and fill it in, or there is no
   connection at all and `get_connection()` yields `None` on every call.
3. **Applying migrations 001-025** in order, for a database that already has data.
4. **Nothing** -- the business date is just today, as of 5 October 2026.
5. **Nightly rates** if the seeded `RoomTypes` prices are wrong for your hotel (§4).

One warning that catches everyone: **never run `database.sql` against a database that
already has data.** It is a fresh-install script. To upgrade an existing database, apply
the numbered files in `migrations/` in order — see AGENTS.md §4.