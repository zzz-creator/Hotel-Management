# AGENTS.md

Guidelines for AI coding assistants working in this repository.

---

## 1. Read first (MANDATORY)

This file is auto-loaded. **The two files below are not.** They hold the per-table schema
reference and the booking/loyalty domain rules, and they are the parts most likely to make
you break something if you guess instead of reading.

**Before your first substantive reply on a task in any area below, read the matching
document, and say which ones you read.**

| Your task touches | You MUST first read |
|---|---|
| Any `.sql` file, `CREATE`/`ALTER`, a column, a table, a migration | **[docs/SCHEMA.md](docs/SCHEMA.md)** |
| `Reservations` / `Rooms` / availability / re-letting / housekeeping status | **[docs/SCHEMA.md](docs/SCHEMA.md)** §2-3 |
| `Transactions`, `Invoices`, `Items`, folio, tax, discounts, room charge, check-out billing | **[docs/SCHEMA.md](docs/SCHEMA.md)** §2, §4 |
| A room **rate** (quoted vs captured, `NightlyRate`, a rate edit) | **[docs/SCHEMA.md](docs/SCHEMA.md)** §2 `Reservations`, **[docs/BOOKING.md](docs/BOOKING.md)** §2 |
| `HotelSettings`, pricing, tax rate, loyalty parameters | **[docs/SCHEMA.md](docs/SCHEMA.md)** §2 |
| "Today" / "today's date" / a report's date window / a day that has closed | **[docs/SCHEMA.md](docs/SCHEMA.md)** §2 `HotelSettings`, **[docs/DEVIATIONS.md](docs/DEVIATIONS.md)** §3 |
| Bulk reset / `delete_all_reservations()` | **[docs/SCHEMA.md](docs/SCHEMA.md)** §5 |
| Booking desk, deposits, prepayments, refunds, booking references | **[docs/BOOKING.md](docs/BOOKING.md)** |
| Loyalty points, tiers, redemption, `LoyaltyAccounts` / `LoyaltyTransactions` | **[docs/BOOKING.md](docs/BOOKING.md)** §5 |
| `CustomerProfiles`, guest login, anything keyed on "who is this guest" | **[docs/BOOKING.md](docs/BOOKING.md)** §1 |
| Guest-facing UI that reveals a room number, stay, or balance | **[docs/BOOKING.md](docs/BOOKING.md)** §1 "Privacy rules" |
| `reports.py` loyalty export | **[docs/BOOKING.md](docs/BOOKING.md)** §5 |
| "Should this app do X?" / a behaviour that looks like a bug but is load-bearing | **[docs/DEVIATIONS.md](docs/DEVIATIONS.md)** — read this **before** changing something that looks wrong |
| The `Reservations.RoomNumber` PK, the `StayLedger` idea, or audit attribution | **[docs/SCHEMA.md](docs/SCHEMA.md)** §4, **[docs/DEVIATIONS.md](docs/DEVIATIONS.md)** §1-2 |
| A fresh install, first login, or "the app is empty — what do I add first?" | **[docs/ONBOARDING.md](docs/ONBOARDING.md)** |

**This file plus the one matching document is your whole required reading for almost every
task.** If a change seems to need more, read the other one too — they total ~600 lines
together, which is cheaper than the bug you are about to introduce.

---

## 2. Project overview

A hotel management console app (Python 3 + SQL Server via `pyodbc`).

### Code

| File | Role |
|---|---|
| `maincopycopy.py` | Entry point and almost everything: menus, reservations, booking desk, billing, loyalty, IT/valet panels |
| `ui.py` | `rich`-based console helpers (menus, tables, prompts, `clear_screen`/`pause`) |
| `reports.py` | CSV report exports, also a standalone CLI |
| `db.py` | Connection string + `get_connection()` context manager |
| `config.ini` | DB connection, hotel, and loyalty defaults. **Untracked** — copy `config.ini.example` |
| `config.ini.example` | The tracked template for the above; blank `password` / `master_secret` |
| `.gitignore` | Keeps `config.ini`, `__pycache__/`, `exports/*.csv` and the generated council artifacts out of history |

New interactive output goes through `ui.py`, never inline `rich`.

### Schema

| File | Role |
|---|---|
| `database.sql` | **Authoritative fresh-install script.** Creates the database if absent, then every table, constraint, index and seed row. Never relies on a migration to produce a table |
| `migrations/001..025_*.sql` | Incremental changes, applied in order, for databases that already exist |
| `docs/SCHEMA.md` | Per-table reference, migration inventory, degradation matrix |
| `docs/DEVIATIONS.md` | Behaviours this app deliberately does **not** have, and what would close each |
| `docs/ONBOARDING.md` | Staff first-run order: bootstrap login, items, prices, rooms, business date |

### Tests (`python -m unittest discover -s tests`; **`pytest` is NOT installed**)

| File | Covers |
|---|---|
| `tests/test_billing_math.py` | Billing arithmetic, captured stay rate, the business date, loyalty calibration, report date windows |
| `tests/test_booking.py` | Booking money rules and the check-out credit |
| `tests/test_customer_loyalty.py` | Customer-keyed loyalty (019) and booking-desk login |
| `tests/test_validate_room.py` | The guest identity check |
| `tests/test_schema_sync.py` | The schema checker itself — a checker that parses nothing must fail |
| `tests/check_schema_sync.py` | `database.sql` vs migrations (a **script**, not a test) |
| `tests/check_migration_sql.py` | Static T-SQL lint (a **script**, not a test) |
| `tests/check_applied_migrations.py` | The **live** database vs `database.sql` (a **script**, not a test) |
| `tests/seed_smoke_test.sql`, `tests/seed_smoke_test_cleanup.sql` | Throwaway populate/undo pair for manual runs |
| `tests/check_docs_sync.py` | This file's staleness checker (a **script**, not a test) |

### Design docs

`PLAN-room-rates-and-folios.md` (room rates, split folio, availability, guest features,
reports), `PLAN-booking-system.md` (public booking desk), `PLAN-loyalty-per-night.md`,
`PLAN-wire-up-rooms.md`, `PLAN-test-plan.md`. Approved designs — read the relevant one
before reworking a feature it covers.

---

## 3. Intentional design decisions (do NOT "fix")

- **Plaintext passwords are intentional.** This is a teaching/demo project.
  `Users.Password`, `CustomerProfiles.Password`, `config.ini` -> `[hotel] master_secret`,
  and all login flows store and compare passwords in plaintext on purpose. Do not introduce
  hashing, salting, or similar security changes without being asked.
- Interactive/UI output uses `logging.info` plus `ui.py` helpers rather than raw
  `print`/`os.system`.
- **Card prompts must never raise.** `luhn_check()` returns `False` for anything that is not
  a pure digit run (rather than letting `int()` raise `ValueError`), and
  `validate_expiration_date()` rejects a month outside `1..12` (so `00/2030` cannot slip
  past the year check). A guest who mistypes has to read "Invalid credit card number", not a
  traceback. `process_credit_card()` stores only the last four digits in `LAST_CARD_DIGITS`
  and clears it on every rejection, so a failed payment can never attribute digits to a
  later one.
- **Master override goes through `require_master_override()`**, not an inline secret check.
  It tries the configured `[hotel] master_secret` first and falls back to the plaintext
  `master` account in `Users`, so a fresh checkout with no configured secret still works.
  Destructive admin actions must call it **and** require an exact typed confirmation — being
  admin is not sufficient on its own.
- **There is no real email or SMS.** `Notifications.Channel` is a label; delivery is
  in-app. `try_open_door()` and "Test Card at Reader" stand in for physical hardware.
- **`GuestRequests` is a dead table** in `database.sql`, referenced by no code. It is a
  pre-015 leftover; `ConciergeRequests` replaced it. Do not wire it up.
- **`config.ini` is untracked on purpose.** It holds the SQL Server password and
  `[hotel] master_secret`. Commit `config.ini.example` (same keys, blank secrets) instead,
  and never `git add -f config.ini`. This does not weaken §3 above: plaintext is a storage
  decision inside the app, not a reason to publish a credential.

---

## 4. Database rules

- **Never execute DDL or modify the database yourself.** Schema changes ship as `.sql`
  files only — either an edit to `database.sql` (fresh installs) **and** a new numbered file
  in `migrations/` (existing databases). Then **tell the user to apply the SQL** in SSMS or
  another tool. Do not run it for them.
- **`database.sql` must be able to build the whole schema on an empty server.** It is the
  authoritative fresh-install script, so it carries a `CREATE TABLE` for **every** table in
  the app. Never write "handled by migration NNN" in it, and never let it depend on a file
  in `migrations/` having been applied: someone running only this script must get the same
  schema a migrated database has. That includes CHECK constraints, DEFAULT constraints,
  indexes and foreign keys, not just tables and columns — the drift that mattered was
  exactly there. It opens by creating the database if absent, so it assumes no `hotelSystem`
  exists.
- **`migrations/` is the upgrade path, not the definition.** Numbered files only change
  databases that already exist. The two are kept in sync by hand, and
  `tests/check_schema_sync.py` fails if they disagree on columns, primary keys and UNIQUE
  indexes. It does **not** compare CHECK, DEFAULT or non-UNIQUE indexes, so a fresh install
  can silently have weaker guarantees than a migrated one; that is why
  `database.sql` got a "structural parity with a migrated database" block. If you change
  schema in one place, change it in the other, and read the migrated database's catalog
  before assuming.
- All SQL in Python uses parameterized queries (`pyodbc` `?` placeholders). Keep it that way.
- New UI helpers go in `ui.py` rather than inline `rich`.

### Adding a migration

1. Read `docs/SCHEMA.md` §1 first.
2. Write `migrations/0NN_<slug>.sql`, numbered **one past the highest existing**. Open with
   a comment saying what it delivers and what it must be applied after.
3. Make it **re-runnable**: guard with `IF NOT EXISTS` / `IF OBJECT_ID(...) IS NULL`, and
   wrap `UPDATE`s in a condition that only fires while the row still holds its old default
   (see how 010 recalibrates tier thresholds).
4. **Mirror the change into `database.sql`** by hand. `tests/check_schema_sync.py` compares
   the two and will fail if you forget — but it compares columns, primary keys and UNIQUE
   indexes only. A CHECK, DEFAULT or non-UNIQUE index you add to one side and not the other
   will not fail any checker, so mirror those by hand and re-read the migration's catalog
   effects.
5. `python tests/check_schema_sync.py` and `python tests/check_migration_sql.py` must both
   exit 0. See §5 for the T-SQL rules they enforce and the ones they cannot.
6. Add the file to the inventory table in `docs/SCHEMA.md`, plus a degradation row if the
   feature can be missing.
7. Update the numbered lists in `docs/SCHEMA.md` §1 if you renumbered anything.
8. Tell the user to apply it. **Do not apply it.**
9. Once the user says they applied it, run `python tests/check_applied_migrations.py`.
   It is the only check that looks at the **live** server rather than the repo, and it
   also calls the real code against it — a column existing is not the same as the
   function that reads it working.

### T-SQL rules the lints enforce

- **A `GO` may never appear inside a `BEGIN...END` block.** `GO` is an SSMS/sqlcmd batch
  separator, so the batch ends there and the parser is left holding an unterminated
  `BEGIN`. Put the `GO` after the `END`. (`check_migration_sql.py`)
- **`dbo.sys.*` is not a thing** → `Msg 208`. Use `sys.columns` / `sys.indexes` /
  `sys.foreign_keys`. (`check_migration_sql.py`)
- **Dynamic SQL goes through `sp_executesql`, never `EXEC`.** `EXEC('literal ' + ...)` is a
  parse error (`Msg 102`) and a bare `EXEC @var` is read as a *module name* (`Msg 203`), so
  the statement text becomes the object being looked up. Always `EXEC sp_executesql @stmt`.
  (`check_migration_sql.py`)

All three reached a live database during migration 019. In each case the batch died, the
intended statement silently did not run, and the *next* statement failed with a message
about something else entirely.

### T-SQL rules the lints cannot enforce

- **Locate a constraint to drop by what it is, never by its name.** Migration 001 declares
  `RoomNumber ... NOT NULL PRIMARY KEY` inline, so SQL Server auto-names it
  `PK__LoyaltyAccounts__<hash>`; a guard testing `name = 'PK_LoyaltyAccounts'` matches
  nothing, silently skips the drop, and the next `ADD CONSTRAINT ... PRIMARY KEY` dies with
  `Msg 1779`. Use `sys.indexes.is_primary_key = 1`, and skip the drop when the desired key
  is already in place.
- `is_primary_key` lives on `sys.indexes`, not `sys.index_columns`.

---

## 5. Conventions

- Top of `maincopycopy.py` has `# type: ignore`; functions are module-level, no classes.
- Match the existing style: string `f`-format logging, `with get_connection() as conn`
  blocks, and the `conn is None` early-return guard after each DB open.
- **Do not add an `except` around the `yield` in `db.get_connection()`.** Catching there and
  yielding a second time is illegal in a generator: Python replaces the real error with
  `RuntimeError: generator didn't stop after throw()`, hiding the actual message. A
  *connection* failure is already handled by `create_connection()` returning `None` (hence
  the `conn is None` guard); a query failure must propagate with its real type and message,
  so a missing migration reads as pyodbc's "Invalid column/object name 'X'". Functions that
  must tolerate an unapplied migration (e.g. the `ReservationArchive` read in
  `search_availability()`) wrap **their own body** in `try/except`.
- `HotelSettings` values are admin-editable free text: every numeric read goes through
  `_setting_float()` / `_setting_int()`, which fall back to the `config.ini` default on a
  blank or non-numeric value. A typo there must never break check-out.

---

## 6. Verification

```powershell
python -m py_compile maincopycopy.py db.py reports.py ui.py   # syntax
python -m unittest discover -s tests                          # unit tests
python tests/check_schema_sync.py                             # schema drift
python tests/check_migration_sql.py                           # T-SQL lint
python tests/check_docs_sync.py                               # docs are stale?
python tests/check_applied_migrations.py                      # live DB matches database.sql
```

Exit code 0 = pass. `check_schema_sync.py` compares the 20 tables that migrations 013-018
`CREATE` (by column, type, and nullability), the columns and primary key that 019, 020 and
022 `ALTER`, and every UNIQUE index any migration creates. **Not covered:** columns that 013
and 018 added to `Transactions` and `Invoices` (those tables are created by 011/012, outside
its window) — if you touch the split-folio or `PrepaidAmount` columns, eyeball `database.sql`
yourself.

`check_applied_migrations.py` is the only check that reads the **live server**, and the only
one that calls the real code against it. It writes nothing, but it does produce CSVs in
`exports/` when it exercises the reports.

Also useful:

```powershell
pip install -r requirements.txt
python maincopycopy.py                                          # run the app
python maincopycopy.py --report transactions --format csv       # report via the app
python reports.py --report loyalty --room 9012                  # report via the CLI
```

`reports.py` exposes a `REPORTS` registry (`transactions`, `reservations`, `loyalty`,
`invoices`, `revenue`, `occupancy`, `housekeeping`, `audit`, `guest_satisfaction`); both
CLIs and the admin "Export Reports" menu dispatch through it, so a new report needs one
entry, not three.

---

## 7. Keeping the docs honest

This file stays short on purpose. Depth lives in `docs/`, and depth rots unless something
notices. When a change lands:

| Change | Update |
|---|---|
| New migration | `docs/SCHEMA.md` §1 inventory + §4 degradation if applicable |
| Column / table / constraint | `docs/SCHEMA.md` §2, **and** `database.sql`, **and** the new migration |
| Booking, refund, deposit, credit rule | `docs/BOOKING.md` §2-4 |
| Loyalty, points, tier, customer identity | `docs/BOOKING.md` §5 |
| A behaviour you chose **not** to implement | `docs/DEVIATIONS.md`, with the change that would close it |
| A first-run or staff-facing procedure | `docs/ONBOARDING.md` |
| New file in the repo | §2 file map above |
| New rule an agent could plausibly undo | §3 (intentional) or §4/§5 (convention) — **not** the docs |

Run `python tests/check_docs_sync.py` before you finish. It fails when a migration is
missing from the inventory, when a function or table named in `docs/` no longer exists,
and when the §2 file map drifts from the repo.

**Apply migrations in order, or re-run `database.sql` on a fresh database.** Migrations
001-025 are all applied to the developer's live database. 001-018 were verified end-to-end
(book → check out with credit → cancel, plus the declined-card and full-refund paths).
019-021 were verified read-only against the catalog, and the flows they back have never been
run against a live database — see `docs/BOOKING.md` §6. 022-025 were verified by
`check_applied_migrations.py` — column shape, the seeded `business_date`, the recalibrated
accrual, the deleted expiry row, and both date reports running — but the database had **no
reservations in it**, so the 022 backfill had nothing to do and the captured-rate read path
has still never seen a real row. `tests/seed_smoke_test.sql` exists for exactly that; the
agent must not run it (see §4).

`database.sql` was audited against the live catalog in October 2026 and the two now agree
on every table, column, type, nullability, primary key, UNIQUE index, CHECK constraint,
DEFAULT constraint, foreign key and secondary index — with one deliberate exception:
`GuestRequests` exists in `database.sql` and not on the live database, because it is the
dead table §3 says not to wire up. That audit fixed a `Transactions.ChargeGroup` declared
`nvarchar(10) NULL` against a real `varchar(10) NOT NULL DEFAULT ('F&B')`, a missing
`Transactions.Description`, six absent column defaults and thirteen absent indexes. The
corrections went into `database.sql` **only**, because the live database already had all of
them and no migration was needed; `database.sql` and `migrations/` therefore differ on
CHECK/DEFAULT/index surface from here on, and `database.sql` is the reference for fresh
installs. Re-run the comparison before trusting either file.
