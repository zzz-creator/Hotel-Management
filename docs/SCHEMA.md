# docs/SCHEMA.md

Table-by-table reference for the hotel database, plus the invariants that are shaped by
the schema rather than by the UI.

**Read this before** touching any `.sql` file, any `CREATE`/`ALTER`, or any code that
reads or writes a column. The rules here are enforced by `tests/check_schema_sync.py` and
`tests/check_migration_sql.py`; run both after any schema edit (see AGENTS.md
"Verification").

Related: [BOOKING.md](BOOKING.md) (booking desk + loyalty domain rules) ·
[../AGENTS.md](../AGENTS.md) (conventions, workflow, verification commands)

---

## 1. Migration inventory

Apply in numeric order. `database.sql` is the equivalent single-file script for a fresh
install and is kept in sync by hand — a schema change lands in **both** places.

| # | File | Delivers |
|---|---|---|
| 001 | `001_loyalty.sql` | `LoyaltyAccounts` + `LoyaltyTransactions` (room-keyed at this point; 019 re-keys it) |
| 002 | `002_transactions.sql` | `Transactions` with `ID` identity PK |
| 003 | `003_reservations.sql` | `CheckInDate`/`CheckOutDate` defaults + date-range CHECK on `Reservations` |
| 004 | `004_align_transactions.sql` | Recreates `Transactions` in the shape the code expects; widens `Reservations.RoomNumber` to `NVARCHAR(50)`. **Refuses to run if `Transactions` has rows** — it is a drop/recreate |
| 005 | `005_pricing_settings.sql` | `HotelSettings` key/value table (admin-editable business rules) |
| 006 | `006_remove_inventory.sql` | Drops the obsolete `Inventory` housekeeping-stock table |
| 007 | `007_loyalty_tiers.sql` | `LoyaltyTiers` (thresholds, accrual multiplier, discount %, perks) |
| 008 | `008_rooms_seed.sql` | Seeds `Rooms` — 150 floors, ~138,180 rooms. Pure seed data |
| 009 | `009_loyalty_nightly_points.sql` | Per-night loyalty settings (`loyalty_points_per_night`, `loyalty_mult_<slug>`); lowers the order-accrual default 10 → 3 |
| 010 | `010_loyalty_tier_rebalance.sql` | Gold 5000 → 2500, Platinum 20000 → 7500. Guarded `UPDATE`s so admin customisations survive |
| 011 | `011_checkout_invoices.sql` | `Invoices`. **Check-out fails until this is applied** — the invoice insert is mandatory |
| 012 | `012_invoice_items.sql` | `Transactions.InvoiceID` + `PaidEarlier` so invoices print itemized |
| 013 | `013_room_types_rates.sql` | `RoomTypes` (nightly rate per category), `Transactions.Description` + `ChargeGroup`, the split folio, and the eight `Invoices` breakdown columns |
| 014 | `014_availability_archives.sql` | `ReservationArchive` so a re-let room does not erase the outgoing stay |
| 015 | `015_guest_requests_orders.sql` | `Orders`/`OrderItems` + `Feedback`, `ConciergeRequests`, `Notifications`, `StaffAlerts`, `Amenities`, `Promotions` |
| 016 | `016_audit_log.sql` | `AuditLog` |
| 017 | `017_door_access.sql` | `KeyCards` + `DoorEvents` |
| 018 | `018_booking_payments.sql` | `ReservationPayments` (signed) + `Invoices.PrepaidAmount` |
| 019 | `019_customer_identity.sql` | Re-keys loyalty to `CustomerID`; adds `CustomerProfiles.Password`/`CreatedAt`, `Reservations.CustomerID`, filtered unique `UX_CustomerProfiles_Email` |
| 020 | `020_partial_booking_credit.sql` | `ReservationPayments.AppliedAmount` + range CHECK, so partial credit is representable |
| 021 | `021_booking_ref_uniqueness.sql` | Filtered unique `UX_ReservationPayments_BookingRef_Charge` over `Deposit`/`Prepayment` |
| 022 | `022_captured_stay_rate.sql` | `Reservations.NightlyRate` so a booked rate is locked at booking time; backfilled from the current category rate, `NULL` left for anything unknown |
| 023 | `023_business_date.sql` | Seeds the (now historical) `business_date` `HotelSettings` row — the app's one-time clock for "which day is it" |
| 024 | `024_loyalty_order_accrual.sql` | Recalibrates `loyalty_accrual_points_per_unit` 3 → 0.5, so F&B cannot out-earn the room |
| 025 | `025_drop_loyalty_expiration.sql` | Deletes `loyalty_expiration_days`, a setting nothing ever read |
| 026 | `026_audit_log_diffs.sql` | `AuditLog.OldValue` / `AuditLog.NewValue` so an update records the before-image, not just a sentence |
| 027 | `027_hotel_settings_keys.sql` | Seeds the `HotelSettings` rows the app reads directly (`hotel_name`, `loyalty_enabled`, `lockout_threshold`, `lockout_duration`, `customer_login_max_attempts`); data only, no DDL |

### Fresh database

Either run `database.sql` once, or apply `migrations/001` … `migrations/027` in order in
SSMS. The migrations are individually re-runnable (guarded by `IF NOT EXISTS` /
`IF OBJECT_ID(...) IS NULL`) so a partially-applied run is a normal state to resume from.

`tests/seed_smoke_test.sql` and `tests/seed_smoke_test_cleanup.sql` are a throwaway
populate/undo pair for exercising the app by hand. They are not part of setup.

### Three ways a migration silently fails

Each of these reached a live database during 019. In every case the batch died, the
intended statement never ran, and the *next* statement failed with an unrelated message.
`tests/check_migration_sql.py` now enforces all three:

1. A `GO` inside a `BEGIN...END` block. `GO` is an SSMS/sqlcmd batch separator, so the
   parser is left holding an unterminated `BEGIN` → `Msg 102` / `Msg 156`. Put `GO`
   after the `END`.
2. `dbo.sys.*` instead of `sys.*` → `Msg 208` at runtime.
3. Dynamic SQL not through `sp_executesql`. `EXEC('literal ' + ...)` is a parse error
   (`Msg 102`) and a bare `EXEC @var` is read as a **module name** (`Msg 203`), so the
   statement text becomes the object being looked up. Always `EXEC sp_executesql @stmt`.

Two more rules that are judgement, not syntax:

- **Locate a constraint to drop by what it is, never by its name.** Migration 001 declares
  `RoomNumber ... NOT NULL PRIMARY KEY` inline, so SQL Server auto-names it
  `PK__LoyaltyAccounts__<hash>`; a guard testing `name = 'PK_LoyaltyAccounts'` matches
  nothing, silently skips the drop, and the following `ADD CONSTRAINT ... PRIMARY KEY`
  dies with `Msg 1779`. Use `sys.indexes.is_primary_key = 1`, and skip the drop when the
  desired key is already in place so the file stays re-runnable.
- **Catalog views are `sys.columns` / `sys.indexes` / `sys.foreign_keys`.** `is_primary_key`
  lives on `sys.indexes`, not `sys.index_columns`.

---

## 2. Tables

### Stays and rooms

**`Reservations`** — `RoomNumber` `NVARCHAR(50)` **PK**, `LastName`, `FirstName`, `Floor`,
`CheckInDate` `date` NOT NULL, `CheckOutDate` `date` NOT NULL, `CustomerID` `int` NULL
(FK → `CustomerProfiles`), `NightlyRate` `decimal(10,2)` NULL (022).

- One reservation row **per room**, so there is no `ReservationID` and no multi-row
  overlap check. Availability works by rule instead: a still-current stay (check-out in
  the future) refuses a new booking; a completed past stay is overwritten. See §3.
- `CheckInDate`/`CheckOutDate` are `date`, not `datetime`, and are NOT NULL with defaults
  plus a date-range CHECK — the code always prompts for them.
- `NVARCHAR(50)` because `Transactions.RoomNumber` has an FK to it.
- `CustomerID` is the link from a stay to its owner and is **cleared to NULL on every
  re-let** (see BOOKING.md).
- `NightlyRate` is the per-night rate **in force when the stay was made**, and it is what
  `post_room_charge()` bills. A rate card is a price list; a confirmed booking is a
  contract, so an admin rate edit after booking must not re-price the stay. `NULL` means
  "never captured" (a pre-022 row, or a category with no rate) and makes check-out fall
  back to the current rate **and say so**; it is never `0`, because `0` would be
  indistinguishable from a genuinely free room. A re-let rewrites the column, because it
  overwrites the row in place.

**`Rooms`** — `RoomNumber` `varchar(10)` PK, `RoomType`, `Description`, `Status`.

- Seeded by 008: floors 1-100 get codes 001-999; floors 101-145 get 001-850; floors
  146-150 get only 951-956. Room numbers are `<floor><3-digit code>`; `RoomType` is
  derived from the floor tier and code segment.
- **There is no FK from `Reservations` to `Rooms`** — room numbers are free text. A
  missing room is auto-registered by `upsert_room_if_missing()` rather than rejected.
- `Status` is active state, not reference data. Valid values are the `ROOM_STATUSES`
  tuple: `Available`, `Occupied`, `Cleaning`, `Dirty`, `Maintenance`.
  Check-in sets `Occupied`; check-out sets `Dirty` **only after the bill is paid**.

**`ReservationArchive`** — 014. `ArchiveID` identity PK plus a snapshot of the outgoing
stay: `RoomNumber`, `Floor`, `LastName`, `FirstName`, `CheckInDate`, `CheckOutDate`,
`Nights`, `RoomType`, `NightlyRate`, `ArchivedAt`.

The archive snapshots `Nights`, `RoomType` and `NightlyRate` alongside the stay dates, so a
later edit to the live row cannot rewrite history. It is best-effort: a missing archive
table logs at debug and never blocks re-booking.

### Folio and money

**`Transactions`** — the guest folio. `ID` identity PK, `RoomNumber` (indexed, FK →
`Reservations.RoomNumber`), `ItemID`, `Quantity`, `UnitPrice`, `Amount`, `CreatedAt`,
`IsBilled`, `InvoiceID` (FK → `Invoices`, NULL until invoiced), `PaidEarlier`,
`Description`, `ChargeGroup`.

- The old `TransactionID` / `ReservationRoomNumber` names are gone (004).
- `Description` labels a line that has no `Items` row — the room charge.
- `ChargeGroup` is `Room` or `F&B`; NULL/`'F&B'` is treated as F&B for pre-013 rows.
- `PaidEarlier` = 1 means paid at order time, 0 means settled at check-out. It is what
  lets an invoice print both in one itemized list.

**`Invoices`** — a permanent snapshot of every settled check-out bill. `InvoiceID`
identity PK, `RoomNumber`, `InvoiceDate`, `Subtotal`, `DiscountCodeAmount`,
`TierDiscountAmount`, `TaxAmount`, `TotalAmount`, `PointsRedeemed`, `RedemptionValue`,
`AmountPaid`, `PrepaidAmount`, plus the 013 split-folio breakdown: `RoomSubtotal`,
`RoomTaxAmount`, `RoomTotal`, `FnbSubtotal`, `FnbDiscountCodeAmount`,
`FnbTierDiscountAmount`, `FnbTaxAmount`.

- `Subtotal`/`TaxAmount`/`TotalAmount` remain **grand totals**. The breakdown columns are
  additional, not a replacement.
- `TotalAmount` is the pre-redemption total; `AmountPaid` is what the card was actually
  charged after redemption (0 for a confirmed $0 bill).
- No FK to room numbers (free text, as with `Transactions`).

**`RoomTypes`** — 013. `RoomType` `varchar(50)` PK, `NightlyRate`, `Description`,
`Active`. The seven categories match `Rooms.RoomType` from 008.

**`Items`** — `ItemID` PK (a plain `int`, **not** identity — you choose it), `Name`
`nvarchar(100)`, `Price decimal(10,2)`, `PricingRule` (`'Peak'`, `'OffPeak'`, or NULL; read by
`get_dynamic_price()`). Sellable items and services live only here. **There is no room-charge
item and there should not be one**: the app posts the room charge itself as a `Transactions`
row with `ItemID` NULL and `ChargeGroup` `'Room'`, so its absence from this table is not a gap.
Every row here is an F&B line.

**`Discounts`** — `Code` PK, `DiscountPercentage`, `CreatedAt`.

**`ValetVehicles`** — `VehicleID` identity PK, `LicensePlate`, `OwnerName`, `ParkingSpot`,
`Status`, `CheckInTime`, `CheckOutTime`. Backs the valet panel.

**`ReservationPayments`** — 018/020/021. **The booking desk's money, deliberately not the
folio.** `PaymentID` identity PK, `RoomNumber`, `BookingRef`, `StayCheckIn`, `Kind`,
`Amount`, `NightsCovered`, `CardLast4`, `PaidAt`, `AppliedToInvoiceID`, `IsApplied`,
`AppliedAmount`, `Notes`.

- **Rows are signed**: `Deposit`/`Prepayment` positive, `Refund`/`Forfeit` negative, so a
  stay's net position is one `SUM()`. `Kind` is CHECK-constrained to the
  `PAYMENT_KIND_*` mirrors.
- Separate from `Transactions` because booking money is not hotel revenue — posting a
  deposit to the folio would inflate `Transactions.Amount`, the invoice subtotal, and
  therefore ADR/RevPAR and the revenue report.
- `StayCheckIn` identifies the stay, because rooms are re-let and keying on
  `RoomNumber` alone would leak one guest's credit onto the next guest's bill.
- Outstanding credit is `SUM(Amount - AppliedAmount)`, **not** `IsApplied`. `IsApplied` is
  a cheap whole-row flag that flips as soon as a row is fully consumed; see BOOKING.md.

**`HotelSettings`** — `SettingKey` PK, `SettingValue` `NVARCHAR(100)`. Admin-editable free
text, so **every numeric read goes through `_setting_float()` / `_setting_int()`**, which
fall back to a built-in default on a blank or non-numeric value. A typo in this
table must never be able to break check-out.

Keys: `business_date`, `peak_factor`, `offpeak_factor`, `tax_rate`,
`loyalty_accrual_points_per_unit` (order/room-service spend only),
`loyalty_redemption_points_per_currency_unit`,
`loyalty_points_per_night`, `loyalty_mult_<room-type-slug>`,
`booking_refund_cutoff_days`, `hotel_name`, `loyalty_enabled`,
`lockout_threshold`, `lockout_duration`, `customer_login_max_attempts`.

Every one of these was asked for (with a default shown) during the first-run wizard's
hotel-settings step and is editable from Admin → Pricing & Settings. `config.ini` carries
no hotel values at all -- only the database connection.

- `business_date` (023) is an ISO `YYYY-MM-DD` **date string, not a number**.
  As of 5 October 2026 nothing reads it: `business_date()` in `main.py` and
  `reports.py` return the wall clock, the admin **Business Date** menu and
  `close_day` are gone, and a report for another day takes an explicit date
  or window (`on_date`, `start_date`/`end_date`). The row lingers; treat it as
  historical. The wall clock is deliberately **not** a night audit — nothing
  bills, expires, cleans or re-rates when the day turns over.
- `loyalty_accrual_points_per_unit` is a **fraction** (0.5), not an integer. It is read
  through `_setting_float()`; reading it as an int turns 0.5 into 0 and silently pays
  nothing for every order. It must stay **below** what a night's stay earns — see
  `points_per_dollar_order_vs_room()`, and BOOKING.md §5.
- `loyalty_expiration_days` **no longer exists** (025). It was seeded, editable on screen,
  and read by nothing. Points do not expire, and there is no longer a control that
  suggests otherwise.

### Guests and loyalty

**`CustomerProfiles`** — `CustomerID` identity PK, `LastName`, `FirstName` (both NOT
NULL), `Email` NULL, `Phone` NULL, `Preferences` NULL, `Password` NULL (plaintext by
design), `CreatedAt`.

`UX_CustomerProfiles_Email` is **unique and FILTERED** (`WHERE Email IS NOT NULL`) and
**must stay filtered**: `check_in()` upserts a name-only profile and leaves `Email` NULL,
and SQL Server allows only one NULL under a plain unique index, so the second walk-in
profile would fail to insert. The index is also the real defence against two accounts
sharing an address, which matters because the booking-desk login is by email.

**`LoyaltyAccounts`** — 019 re-keyed this. `CustomerID` **PK** (FK →
`CustomerProfiles.CustomerID`), `RoomNumber` `NVARCHAR(50)` **NULL** (a "last room seen"
display pointer, no longer a key and no longer unique), `Points`, `Tier`, `LastUpdated`.

**`LoyaltyTransactions`** — `ID` identity PK, `CustomerID` NULL, `RoomNumber` NULL,
`Delta`, `Reason`, `CreatedAt`, `SourceID` `NVARCHAR(100)` NULL.

`SourceID` is the idempotency guard for every award (see BOOKING.md). `RoomNumber` is the
room points were *earned* in and is **never rewritten** by a room move.

**`LoyaltyTiers`** — 007. `TierName` PK, `MinLifetimePoints`, `PointsMultiplier`,
`DiscountPercent`, `Perks`. Current thresholds: Bronze 0 / Silver 1000 / Gold 2500 /
Platinum 7500; `DEFAULT_TIERS` in the code matches.

**`Users`** — `Username` PK, `Password` (plaintext by design), `FailedAttempts`,
`LockoutTime`, `Role`.

### Guest services

**`Orders`** — `OrderID` identity PK, `RoomNumber`, `Status`, `PlacedAt`, `UpdatedAt`,
`CompletedAt`, `Notes`. Lifecycle `Placed → Preparing → Ready → Delivered → Completed`
with `Cancelled` reachable at any point, CHECK-constrained so a typo cannot invent a state
the UI cannot render. `ORDER_STATUSES` is the Python mirror.

**`OrderItems`** — `OrderItemID` identity PK, `OrderID`, `ItemID`, `ItemName` (denormalised
so the order still reads if the item is renamed), `Quantity`, `UnitPrice`.

**`Feedback`** — `FeedbackID` identity PK, `RoomNumber`, `LastName`, `FirstName`, `Rating`
(CHECK 1-5), `Comments`, `CreatedAt`.

**`ConciergeRequests`** — `RequestID` identity PK, `RoomNumber`, `LastName`, `FirstName`,
`Message`, `Status` (`Open` / `In Progress` / `Resolved`), `Response`, `CreatedAt`,
`ResolvedAt`, `ResolvedBy`.

**`Notifications`** — `NotificationID` identity PK, `RoomNumber`, `Message`, `Channel`
(`In-Room` / `SMS` / `Email`), `SentBy`, `CreatedAt`, `ReadAt`. There is **no real email
or SMS** — the channel is a label only and delivery is in-app.

**`StaffAlerts`** — `AlertID` identity PK, `TargetRole`, `Message`, `Severity` (`Info` /
`Warning` / `Critical`), `CreatedAt`, `AcknowledgedAt`, `AcknowledgedBy`.

**`Amenities`** — `AmenityID` identity PK, `Name`, `Description`, `DisplayOrder`, `Active`.

**`Promotions`** — `PromotionID` identity PK, `Title`, `Details`, `DiscountCode`,
`StartsOn`, `EndsOn`, `Active`.

**`KeyCards`** — 017. `CardID` identity PK, `CardNumber` (`KC-` + six random digits,
probed for uniqueness), `RoomNumber`, `LastName`, `FirstName`, `Status`
(`Active`/`Revoked`/`Lost`/`Expired`, enforced by `CK_KeyCards_Status` so a typo cannot
invent a fifth state), `IssuedAt`, `ExpiresAt`, `IssuedBy`, `RevokedAt`,
`RevokeReason`. A card grants access only while `Status = 'Active'` **and** today is
within `[IssuedAt, ExpiresAt]`.

**`DoorEvents`** — 017. `EventID` identity PK, `CardNumber`, `RoomNumber`, `Result`
(`Granted`/`Denied`), `Detail`, `EventTime`. Append-only; every door read writes one,
**including denials**, which is what makes the log worth having.

### Audit

**`AuditLog`** — `AuditID` identity PK, `CreatedAt`, `Username`, `Action`, `EntityType`,
`EntityID`, `Details`, `OldValue`, `NewValue` (026).

For value changes the before/after belongs in `OldValue`/`NewValue`; the caller
passes it, because only it knows what changed -- for a re-let the old row has
already been archived by the time `log_audit()` runs. The two columns are NULL
for actions that have no before-image (CREATE, LOGIN), so the absence of a diff
means "no diff exists", not "the value was blank".

`log_audit()` opens its **own** connection on purpose, so a business transaction rolling
back cannot erase its audit trail, and it never raises — a missing table logs at debug
only. `Username` comes from the module-level `CURRENT_USER`, which `admin_login()` sets and
which is **not** reset on logout: treat it as the last-logged-in user, not a session token.

### Dead table

**`GuestRequests`** exists in `database.sql` and is referenced by **no code**. It is a
leftover from the pre-015 simulated concierge. Do not wire it up; `ConciergeRequests`
replaced it.

---

## 3. Availability without overlap checks

`Reservations.RoomNumber` is the primary key, so a room has at most **one** live booking
and re-booking silently overwrote the previous guest. A `ReservationID` identity plus real
overlap queries would be the proper fix, but it would rewrite `add_reservation()`,
`edit_reservation()`, check-in/out, loyalty and invoice keying, and every
`WHERE RoomNumber = ?` site. Instead:

- `add_reservation()` copies the outgoing **completed past** stay into
  `ReservationArchive` first, via `archive_reservation()`.
- Availability therefore reads **live + archived** (`search_availability()`).
- Stays are **half-open** `[check_in, check_out)`, so a guest departing on the 4th can be
  replaced by one arriving on the 4th. `stays_overlap()` is the pure single source of
  truth for that rule and is unit-tested; the SQL in `search_availability()` only mirrors
  it to pre-filter rows.
- A **cancelled booking is deliberately not archived** — `ReservationArchive` feeds
  availability search, so archiving a stay that never happened would mark the room busy for
  dates nobody occupied. The signed `Refund`/`Forfeit` row is the cancellation's history.

`add_reservation()` and `edit_reservation()` refuse rooms whose status is `Maintenance`.

---

## 4. Graceful degradation

Anyone cloning onto a fresh database applies migrations in order, and a partially-migrated
database is a normal state during that run. These fallbacks are load-bearing.

| Missing | Behaviour |
|---|---|
| 011 | **Check-out fails.** The invoice insert is mandatory; there is no fallback |
| 013 | `get_nightly_rate()` falls back to `DEFAULT_ROOM_TYPE_RATES`; `ensure_room_types_seeded()` inserts only *missing* categories at start-up |
| 014 | `archive_reservation()` logs at debug and no-ops; re-booking still works |
| 015 | `order_item()` and `add_order_items()` no-op gracefully; feedback/concierge/notification/amenity/promotion features log at debug |
| 016 | `log_audit()` logs at debug and no-ops |
| 017 | Key-card issue/move/revoke/door-read log at debug and no-op |
| 018 | The booking desk still books a room, but a deposit or prepayment **cannot be recorded** — say so rather than pretending the money was taken. Check-out bills in full: `_build_invoice_insert()` omits `PrepaidAmount` when `_invoices_have_prepaid_column()` is false |
| 019 | `customer_id_for_stay()` returns `None` → **zero points, no tier, no award**. Never an error, and never "credit the room". All callers log at debug and degrade, so check-out works with no loyalty. The loyalty **report** has no fallback and errors |
| 020 | The legacy whole-row credit path runs, latched by `_RESERVATION_PAYMENTS_PARTIAL_SUPPORT`. `_set_partial_credit_unsupported()` latches **only** on a missing column or table — never on a deadlock — so a transient error cannot pin the process to the legacy path |
| 021 | Nothing breaks; `new_booking_ref()` falls back to its racy `booking_reference_exists()` probe, and a collision raises `BookingRefTaken` for `_write_booking_charge()` to retry |
| 022 | `post_room_charge()` bills the **current** category rate, exactly as it did before, and logs that no rate was captured. `Reservations.NightlyRate` stays `NULL` for new stays. Not an error: an unapplied 022 must not stop a guest checking out |
| 023 | Nothing breaks: as of 5 October 2026 `business_date()` is the wall clock, and every report still runs. What is not reproducible is re-running a historical day without passing its explicit date/window |
| 024 | Nothing breaks. The order rate is simply still whatever the operator set; `points_per_dollar_order_vs_room()` reports the inversion on the settings screen rather than failing |
| 025 | Nothing breaks. The `loyalty_expiration_days` row lingers, and nothing reads it |

### Reports have no fallback — on purpose

`export_invoices`, `export_revenue`, `export_occupancy`, `export_audit_log`, and
`export_guest_satisfaction` raise a real pyodbc "Invalid column/object name" until their
migration lands (`export_occupancy` needs `Invoices.RoomSubtotal` for ADR/RevPAR). A report
that quietly omits half its columns is worse than one that refuses to run.

### Known constraint: `Reservations.RoomNumber` as the primary key

**Not a degradation — a standing limitation, recorded here so it is not rediscovered as a
bug.** `Reservations` is keyed on `RoomNumber` alone, so a room can hold **at most one stay
row**, ever. Consequences that follow from the key and cannot be fixed in application code:

- Overlapping stays in one room are **not representable**. A guest arriving on the 4th
  while another departs on the 6th cannot both exist. Availability enforces this by rule
  (§3) rather than by an overlap constraint, because there is nothing to overlap against.
- Re-letting a room **destroys the previous stay's live row**. The stay is copied to
  `ReservationArchive` first, which is why history and past-occupancy search survive — but
  the archive is a *snapshot*, not a second live row.
- Anything that queries "who is in this room" against `Reservations` sees the **current**
  guest only. This is a live limit on the housekeeping board and on `export_housekeeping`,
  whose "current guest" column is therefore the current occupant and never a past one.
- `ReservationPayments` joins back on `(RoomNumber, StayCheckIn)` for exactly this reason:
  the room is not a unique stay identifier, so the pair is.

**The fix is an append-only `StayLedger`** — a separate table where each stay is a new row
with its own surrogate key, so a room can hold many non-overlapping stays and the archive
becomes unnecessary. That is the only shape that actually removes the limitation.

It is **deliberately not done here.** It rewrites `_book_reservation_in_conn()`, the one
function in this codebase whose transaction handling is unambiguously correct: it claims a
room, archives the outgoing stay, and writes the incoming one on a single connection with
**no commit**, so a declined card cannot leave a room reserved. A correct, load-bearing
transaction should not be replaced as collateral damage of a schema change, and there is
no test in this repo that runs the full book → check out → cancel path against a live
database. Fix it as its own change, with database-backed tests, not as a side effect.

Audit-trail diffs: `AuditLog.OldValue` / `AuditLog.NewValue` (026) now record the
before/after for value changes, at the call sites that have the old value in
scope (`set_setting`, `set_room_status`, `update_room_type_rate`, discount edits). The
remaining sites still log only the free-text `Details`; opt them in the same way
when the before-image is cheap to read.

---

## 5. Bulk reset

`delete_all_reservations()` is an **admin-only** destructive action ("Destructive" section
of the admin panel), gated on `require_master_override()` **plus** an exact typed phrase
(`RESERVATION_RESET_CONFIRM`). Being admin is not sufficient on its own. Two scopes live
in `RESERVATION_RESET_SCOPES`: stay data only (keeps `Invoices`, `LoyaltyTransactions`,
`CustomerProfiles`) or the full financial wipe.

Four rules it must keep honouring:

- **Order is load-bearing.** `Transactions.RoomNumber` is the only FK pointing at
  `Reservations.RoomNumber`, so child tables must be emptied before `Reservations` or SQL
  Server raises error 547. `Transactions` is in both scope lists for that reason.
- **One transaction, one commit.** Every `DELETE` runs on a single connection and commits
  once, so a failure part-way leaves the database untouched; the context manager closing
  without a commit is what performs the rollback.
- **Only tables that exist.** `_existing_tables()` filters the scope list through
  `sys.tables`, so an unapplied 014-021 is skipped and reported rather than erroring.
  `AuditLog` is deliberately preserved — it records the reset.
- **`Rooms.Status` is reset to `Available`**, since a room left flagged `Occupied`/`Dirty`
  with no reservation would misreport the housekeeping board.
