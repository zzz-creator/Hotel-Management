# PLAN — Room Rates, Split Folios, Availability & Door Access

Five phases, migrations 013-017. Applies after `012_invoice_items.sql`.
Every phase is additive; nothing existing is removed.

## Motivation

The app bills room service but **never charges for the room**. `Reservations` has no
rate column (`database.sql:92`), `Items` holds only sellable items/services, and
`bill_room_transactions()` (`maincopycopy.py:2570`) only sums `Transactions` — which
only ever contains `order_item()` rows. A 14-night stay with no room service produces
a **$0.00** bill. Room-category multipliers exist but scale only *loyalty points*,
never a price. No nightly rate exists anywhere in the project.

## Design

### Folio split (rooms vs F&B)

- `Transactions.ChargeGroup` (`'Room'` / `'F&B'`, default `'F&B'`) tags every line.
- Discount code and loyalty tier discount apply to the **F&B group only** — they are
  promotional, room rates are contractual, and the tier perk text literally reads
  "5% off room service". **The room charge is never discounted.**
- **Both** groups are taxed, each at the existing single `get_tax_rate()`.
- Point redemption still applies to the **combined** post-tax total.
- `Invoices.TotalAmount` / `AmountPaid` stay **grand totals** so existing reports and
  `print_invoice()` totals keep working; the new per-group columns carry the split.
- **Double-earn guard**: `award_billed_order_points()` must filter
  `ChargeGroup = 'F&B'`. Without it a guest earns points twice for one stay — once via
  `award_stay_points()` for the nights, again via the room charge treated as spend.

### Rates

- `RoomTypes` is the single source of nightly price, seeded with the 7 categories in
  `DEFAULT_ROOM_TYPE_MULTIPLIERS` (Standard, Deluxe, Junior Suite, Suite, Grand Suite,
  Penthouse, Presidential Suite) so rates and seeded `Rooms.RoomType` values line up.
- Rates are read at **billing** time (chosen over a per-booking snapshot), but the room
  charge is posted as a **folio line** — which snapshots the price. Editing a rate later
  never rewrites a past invoice.
- `post_room_charge()` is idempotent on the folio line itself
  (`Description = 'Room charge for {check_in}'`), since the guard has to survive the line
  being billed and disappears from the loose-transaction set. Mirrors the idempotency
  intent of `award_stay_points()`.
- Nights = `stay_nights(CheckInDate, CheckOutDate)`, i.e. whole **calendar** nights
  (arrive 01 Mar 15:00, leave 04 Mar 11:00 = 3), floored at 0, so the room charge, the
  invoice and `award_stay_points()` all agree. Charged at **check-out**, not check-in, so
  a declined card leaves the charge unbilled and retryable.
- Checked in early: still billed the reserved nights. (Flag for policy decision.)

### Availability without a remodel

`Reservations.RoomNumber` is the PK, so a room has at most one live booking and
re-booking silently overwrites the previous stay. The correct fix is a
`ReservationID` identity plus real overlap checks, but that rewrites
`add/edit/delete_reservation`, `check_in`/`check_out`, loyalty and invoice keying, and
every `WHERE RoomNumber = ?` site.

**Chosen instead**: keep the PK and copy the outgoing completed stay into
`ReservationArchive` when `add_reservation()` overwrites it. Availability search then
reads live + archived. Zero existing query sites change.

### IT panel

Left as simulation per user decision; only its raw `logging.info` menus are converted
to `ui.show_menu` so it stops being the one panel bypassing `ui.py`.

## Changes

- [x] `migrations/013_room_types_rates.sql`: create `RoomTypes`; add
      `Transactions.Description`, `Transactions.ChargeGroup`; add the 7
      per-group `Invoices` columns.
- [x] `migrations/014_availability_archives.sql`: create `ReservationArchive`.
- [x] `migrations/015_guest_requests_orders.sql`: create `Feedback`,
      `ConciergeRequests`, `Notifications`, `StaffAlerts`, `Orders`, `OrderItems`,
      `Amenities`, `Promotions`.
- [x] `migrations/016_audit_log.sql`: create `AuditLog`.
- [x] `migrations/017_door_access.sql`: create `KeyCards`, `DoorEvents`.
- [x] `database.sql`: add all of the above for fresh installs.
      (`tests/check_schema_sync.py` asserts it agrees with 013-017.)

### Phase 1 — rates & split folio

- [x] `maincopycopy.py` constants: `DEFAULT_ROOM_TYPE_RATES`.
- [x] Getters/setters: `get_room_types()`, `get_nightly_rate(room_type)`,
      `update_room_type_rate()`, `ensure_room_types_seeded()` (called beside
      `ensure_loyalty_tables()` in `main()`).
- [x] `post_room_charge(room_number, check_in, check_out)` — inserts
      `ItemID NULL, Quantity = nights, ChargeGroup = 'Room', Description = ...`.
- [x] `check_out()` — call `post_room_charge()` **before**
      `bill_room_transactions()`.
- [x] `bill_room_transactions()` — split subtotal by `ChargeGroup`, run
      `compute_and_apply_discounts()` on F&B only, tax each group, redemption over
      the combined total, write the new invoice columns.
- [x] `award_billed_order_points()` — filter to `ChargeGroup = 'F&B'`.
- [x] `print_invoice()` — `COALESCE(it.Name, t.Description, 'Charge')`;
      render separate "Room Charges" and "Food & Beverage" tables; new group columns
      in the `.txt` export.
- [x] `manage_pricing_rules()` — new "Room Types & Rates" screen
      (`view_room_type_rates()`).

### Phase 2 — availability & arrivals

- [x] `add_reservation()` — archive the outgoing completed stay before overwrite.
- [x] `search_availability(check_in, check_out, room_type=None, floor=None)` — free if
      no live row, or the live row ends before the window opens / starts after it
      closes, and `Rooms.Status <> 'Maintenance'`. Reads live + archived. The rule is
      the pure `stays_overlap()` helper; SQL only pre-filters.
- [x] `arrivals_departures_board(date)` — arrivals / in-house / departures; wired into
      `admin_panel()` and `rooms_admin_menu()`.
- [x] Delete the orphan `data.json` (hardcoded guest names, read by nothing).

### Phase 3 — real features (drop the simulation)

- [x] `track_order_status()` — drop `random.choice`; real
      Placed -> Preparing -> Ready -> Delivered -> Completed/Cancelled lifecycle
      advanced by staff.
- [x] `order_item()` — create the `Orders` + `OrderItems` rows on every success path.
- [x] `provide_feedback()` — persist the rating/comment.
- [x] `contact_concierge()` — drop `time.sleep(2)`; file a tracked request.
- [x] `send_notification_to_customer()` / `send_alert_to_staff()` —
      insert rows instead of printing; new "Guest Requests" and "Alerts" admin
      screens with acknowledge/resolve.
- [x] `view_amenities()` / `view_promotions()` — read editable tables.

### Audit (built in Phase 1, reported in Phase 4)

- [x] `log_audit(action, entity_type, entity_id, details)` helper.
- [x] Instrument: reservation add/edit/delete, user CRUD, item CRUD, discount CRUD,
      room status changes, rate changes, loyalty adjustments, key-card changes.

### Phase 4 — reports

- [x] `reports.py`: `export_invoices`, `export_revenue` (by day + room type),
      `export_occupancy` (occupancy %, ADR, RevPAR), `export_housekeeping`,
      `export_audit`, `export_guest_satisfaction`.
- [x] Register in `export_reports_menu()` and both argparse `--report`
      choice lists — all three dispatch through the `REPORTS` registry.

### Phase 5 — door access

- [x] `issue_key_card()` in `check_in()`; `revoke_active_key_cards()` in `check_out()`
      and `delete_reservation()`.
- [x] `edit_reservation()` — re-point the card beside the existing child-row
      moves (`move_key_cards()`).
- [x] `door_access_menu()` — staff issue / revoke / report lost / view; guest
      "My Key Card".
- [x] Fill the empty `# Door Access Control` section header.

### Tidy-up

- [x] `it_support_panel()` — convert raw `logging.info` menus to `ui.show_menu`.
- [x] `AGENTS.md` — migrations list, schema expectations, folio model, all in sync.
- [x] `tests/test_billing_math.py` — pure helpers only (nights calc, per-group
      discount/tax, rate lookup, availability overlap), no DB. 35 tests.
- [x] `tests/check_schema_sync.py` — `database.sql` vs migrations 013-017.

## Remaining (after user applies SQL)

- User runs `migrations/013` through `017` in order in SSMS. **Check-out fails until
  013 is applied** (the folio split and room charge depend on it).
- Verify with: a multi-night booking with no room service must now produce a
  non-zero invoice whose Room Charges total equals `nights x nightly rate`.
- Optional: run "Recalculate All Tiers" (admin) after 013.

## Out of scope

- Reserving the `ReservationID` remodel (deliberately deferred; see Design).
- Renaming or dropping `Inventory`-era naming, unrelated cleanup.
- Anything security related (plaintext passwords stay, per AGENTS.md).
- Real email/SMS delivery for notifications (still in-app only).
- Late-checkout / no-show / deposit policy rules.
