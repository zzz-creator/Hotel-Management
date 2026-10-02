# PLAN — Wire up the `Rooms` table

Status: Approved (plan only, no coding yet)
Created: 2026-09-15

## Context (from reading)

- `Rooms` (RoomNumber varchar(10) PK, RoomType, Description, Status default 'Available') exists in `database.sql`; zero code references it.
- Room number format used by the app: `<floor><3-digit-code>` (e.g. `9012` = floor 9, code 012). Existing reservations in `data.json` use values like `9012`, `9326`, `9756`, `1111` — the seed data must include these so status tracking never orphans them.
- `check_in()` (maincopycopy.py:1857) and `check_out()` (maincopycopy.py:2073) are 100% simulated — no DB writes.
- `add_reservation()` (maincopycopy.py:518) only checks "room already has a reservation" (PK), never date ranges, never `Rooms` status.
- AGENTS.md rules: DDL/DML must ship as a numbered `.sql` in `migrations/` and be applied by the user in SSMS — do not run it yourself. All Python SQL stays parameterized.

## Deliverables

### 1. `migrations/008_rooms_seed.sql` (new file, zero DDL — pure seed data)
- Idempotent set-based INSERT (numbers CTE + `NOT EXISTS`, matching `007_loyalty_tiers.sql` idempotency).
- **150-floor tower, ~138k rooms** (reduced upper tiers): floors 1-100 get codes 001-999, floors 101-145 get 001-850, floors 146-150 get only codes 951-956. Room type derived from floor tier + code segment (`Standard`/`Deluxe`/`Junior Suite`/`Suite`/`Grand Suite`/`Penthouse`/`Presidential Suite`).
- **Must include every room number already present in `Reservations`** (from `data.json`: `9012, 9015, 9016, 9017, 9020, 9063, 9072, 9326, 9732, 9756, 1111`) — the peer-review "data audit / reconciliation" fix.
- No schema changes (no `BasePrice`/`Floor` columns — keeping scope tight per chairman; the floor is already derivable from the room number). `RoomType` is the pricing/board driver for now.
- Tell the user to run it in SSMS before testing.

### 2. Python room helpers (in `maincopycopy.py`, near the settings helpers)
- `ROOM_STATUSES = ("Available", "Occupied", "Cleaning", "Dirty", "Maintenance")` constant.
- `get_room_status(room)` → returns status or `None`.
- `upsert_room_if_missing(room)` → auto-creates a minimal `Available` row when a reservation/check-in references a room not in `Rooms` — guard that keeps flows from crashing and keeps the board complete.
- `room_has_date_conflict(room, check_in, check_out)` → `SELECT 1 FROM Reservations WHERE RoomNumber=? AND CheckInDate < ? AND CheckOutDate > ?` (the council's "date-range problem, not a status problem" fix). Used by add/edit for overlap safety.
- Dispatch helpers for the admin rooms menu.

### 3. Date + status validation on reservation create/edit (non-negotiable, per chairman)
- `add_reservation()`: after format validation, run `room_has_date_conflict()` and refuse on overlap; refuse booking a room whose `Rooms.Status` is `Maintenance`. Keep existing PK check.
- `edit_reservation()`: when the room/date changes, run the same overlap check and refuse conflicting edits. Keep the existing transaction/loyalty row-move logic untouched.

### 4. Check-in / check-out now write real state
- `check_in()`: on successful validation, verify **today is within the reservation's `CheckInDate..CheckOutDate` window** (new guard), then `UPDATE Rooms SET Status='Occupied'` (calling `upsert_room_if_missing` first if the room row is absent — graceful fallback, never a crash).
- `check_out()`: on matched reservation, `UPDATE Rooms SET Status='Dirty'` (same upsert guard).

### 5. Rooms Status board in the admin panel
- Admin menu: insert a new `---- Rooms ----` section — add option `21. Rooms & Housekeeping` (submenu: `1. View Rooms`, `2. Update Room Status`, `3. Back`), renumber Exit to `22`. Dispatch `elif choice == '21'` → `rooms_menu()`. Minimal diff: menu array + dispatch lines.
- Manager menu: same submenu view-only (`14. Rooms & Housekeeping`; Exit renumbered to `15`).
- `rooms_menu()` uses `ui.show_table` with a parameterized `SELECT r.RoomNumber, r.RoomType, r.Description, r.Status, res.LastName AS Guest` `LEFT JOIN Reservations res ON res.RoomNumber = r.RoomNumber`, filtered by floor (required) and optional room type — at 138k rooms the board must never render everything.
- Update-status flow validates input against `ROOM_STATUSES`, uses parameterized `UPDATE`.

### 6. Docs & verification
- Update `AGENTS.md`: document `Rooms` as active (status board + check-in/out sync), add `008` to the pending-migrations list.
- Verify: `python -m py_compile maincopycopy.py db.py reports.py ui.py`, then a manual smoke run.
- Explicitly **out of scope** (chairman: don't promise): housekeeping lifecycle, RevPAR/revenue report, `CustomerProfiles` wiring, `BasePrice` DDL.

## Risks & mitigations
- **Existing reservations reference rooms not in `Rooms`** → seed includes all `data.json` rooms + `upsert_room_if_missing` guard.
- **`Rooms` table empty on live DB** → `008` must be applied first; upsert guard prevents app crashes if it isn't.
- **Renumbered Exit options** → only the admin (21→22) and manager (14→15) menus move; contained 2-line edits per role; staff menu untouched.