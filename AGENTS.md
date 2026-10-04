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
| `main.py` | Entry point and almost everything: menus, reservations, booking desk, billing, loyalty, IT/valet panels, first-run onboarding |
| `ui.py` | `rich`-based console helpers (menus, tables, prompts, `clear_screen`/`pause`) |
| `reports.py` | CSV report exports, also a standalone CLI |
| `db.py` | Connection string + `get_connection()` context manager |
| `config.ini` | DB connection, hotel, and loyalty defaults. **Untracked** — copy `config.ini.example` |
| `config.ini.example` | The tracked template for the above; blank `password` / `master_secret` |
| `.gitignore` | Keeps `config.ini`, `__pycache__/`, `exports/*.csv` and the generated council artifacts out of history |
| `.gitattributes` | Pins LF in the repository and native endings in the working tree, so `core.autocrlf` stops deciding per machine |
| `README.md` | What the project is, requirements, setup, tests, and the "not production software" warnings. The first thing anyone landing on the repo reads |
| `LICENSE` | AGPLv3, verbatim from gnu.org. §13 is why this is AGPL and not GPL — a modified copy served over a network must offer its source |

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
| `tests/test_onboarding.py` | First-run wizard: the completion marker, the first-account guard, item/room seeding idempotency, room-layout bounds |
| `tests/test_schema_sync.py` | The schema checker itself — a checker that parses nothing must fail |
| `tests/check_schema_sync.py` | `database.sql` vs migrations (a **script**, not a test) |
| `tests/check_migration_sql.py` | Static T-SQL lint (a **script**, not a test) |
| `tests/check_applied_migrations.py` | The **live** database vs `database.sql` (a **script**, not a test) |
| `tests/verify_e2e.py` | Builds a **disposable** database, runs both install paths, exercises the booking code, drops it (a **script**, not a test) |
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
  admin is not sufficient on its own. Both onboarding paths create that account with the
  **admin** role, not a role called `master`: the override matches on the username alone, while
  `admin_panel()` has no `master` branch and `add_user()` only offers admin/staff/manager, so
  a `master`-role row would be an account nobody can sign in to.
- **There is no real email or SMS.** `Notifications.Channel` is a label; delivery is
  in-app. `try_open_door()` and "Test Card at Reader" stand in for physical hardware.
- **The first-run wizard creates an administrator without authenticating anyone, and that
  is not an oversight.** It runs from `main()` when the `onboarding_complete` setting is
  absent, which is the correct reading for a fresh database because nothing seeds that row.
  There is no account to authenticate *against* at that point, so the trust assumption is
  the one the database already makes: whoever is at the console can open SSMS on the empty
  database. Two things keep it from becoming a hole, and neither is optional —
  `create_first_user()` refuses outright if **any** account already exists, and once
  `mark_onboarding_complete()` has run the wizard never runs again, so the Admin Panel's own
  `add_user()` is the only remaining path. Do not add a "re-run setup" shortcut to the main
  menu, and do not move the checklist there: it writes accounts, and the main menu is the
  guest-facing surface. It belongs in the Admin Panel, where login has already happened.
- **The setup checklist derives its status from live queries, not from the marker.** The
  marker answers "has a human been through the wizard"; the checklist answers "can the front
  desk take a booking a guest can pay for", which is the question the runbook actually cares
  about. Someone who inherited a database and never ran the wizard still needs the second
  one answered. Every probe swallows its own failure so an unapplied migration costs one red
  row rather than a dead screen, and each probe distinguishes *unreachable* from *empty*.
- **The room seeder is capped at 150 floors and 999 rooms per floor**, and those are the
  app's limits, not a round number: `view_rooms()` rejects any floor outside 1-150, and both
  it and the housekeeping report recover the floor as `LEFT(RoomNumber, LEN(RoomNumber) - 3)`
  over a `<floor><3-digit code>` room number. Seeding past either bound would create rooms the
  UI cannot reach. `validate_room_layout()` is pure and holds the bounds; the wizard and
  `seed_rooms()` both go through it.
- **There is deliberately no "Room Charge" item in `Items`, and adding one is a bug, not a
  missing feature.** The app posts the room charge itself at check-out: a `Transactions` row
  with `ItemID` **NULL** and `ChargeGroup` `'Room'`, guarded against posting twice. Because
  `record_transaction_for_room()` defaults `ChargeGroup` to `'F&B'`, an orderable "Room Charge"
  item would post a second charge on top of the automatic one *and* accrue loyalty points on it
  via `award_billed_order_points()`. This is load-bearing in two directions: never add such an
  item, and do not "fix" `Items` by assuming every charge needs a row there. It is also what
  lets `COUNT(*) FROM Items > 0` stand as the catalogue-readiness test — every row in `Items` is
  a genuine sellable line. `StarterCatalogueContentTests` pins all of this.
- **`add_custom_item()` assigns `ItemID` as `MAX(ItemID)+1`; `add_item()` asks for it.** Not an
  inconsistency to reconcile. `Items.ItemID` is a plain `int` PK, not identity, so a typed id
  can collide with an existing row and hand the operator a constraint-violation traceback.
  Auto-assigning makes that unreachable; `add_item()` keeps the manual path for anyone who wants
  a deliberate numbering scheme. `add_custom_item()` validates name length against the
  `nvarchar(100)` column and re-asks on an unparseable price, because a mistyped prompt must not
  produce a traceback (the same rule as the card prompts in §3).
- **`GuestRequests` is a dead table** in `database.sql`, referenced by no code. It is a
  pre-015 leftover; `ConciergeRequests` replaced it. Do not wire it up.
- **`config.ini` is untracked on purpose.** It holds the SQL Server password and
  `[hotel] master_secret`. Commit `config.ini.example` (same keys, blank secrets) instead,
  and never `git add -f config.ini`. This does not weaken §3 above: plaintext is a storage
  decision inside the app, not a reason to publish a credential.

---

## 4. Database rules

- **Never execute DDL or modify the live database yourself.** Schema changes ship as `.sql`
  files only — either an edit to `database.sql` (fresh installs) **and** a new numbered file
  in `migrations/` (existing databases). Then **tell the user to apply the SQL** in SSMS or
  another tool. Do not run it for them.
- **A scratch database is not the live database, and writing to one is permitted.** The rule
  above exists to protect rows you cannot get back. It has no force against a throwaway, so it
  must not be read as a blanket ban on touching SQL Server: read that way it forbids the only
  way to find out whether a migration works. A **scratch** database — one whose name comes
  from a config key that is **not** the one `[database]` uses — may be created, built from
  `database.sql`, migrated through every file in `migrations/` in order, seeded, exercised by
  the real code, and dropped. Three conditions, all non-negotiable:
  1. The live database's name must never appear in a scratch connection string.
  2. The script must refuse to run if it would resolve to the `[database]` target, rather than
     trusting the config file to be correct.
  3. Keep it in one script. `tests/verify_e2e.py` is where this is meant to live; ad-hoc
     `sqlcmd` against a scratch database is not the sanctioned path and leaves nothing behind
     that the next agent can re-run.

  This is the only sanctioned way to exercise unapplied migrations, and the reason it is
  written down is that not having it written down is what left 019-021 unrun. Migration 019
  died three separate times against a live server (`dbo.sys.*`, bare `EXEC`, `GO` inside
  `BEGIN`) and passed `check_migration_sql.py` as written: code you never ran has never met a
  real constraint it did not expect, and no static check can substitute for running it.
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
8. Tell the user to apply it. **Do not apply it to the live database** — a scratch database is
   the one exception, and only under the rule at the top of this section.
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
- **A filtered index's `WHERE` cannot join two equality predicates with `OR`.**
  `WHERE ([Kind]='Deposit' OR [Kind]='Prepayment')` fails with `Msg 156, Incorrect syntax
  near the keyword 'OR'`; `WHERE [Kind] IN ('Deposit', 'Prepayment')` is accepted. This
  shipped once: `database.sql` carried the `OR` form while `migrations/021` used `IN`, so an
  upgraded database had the index and a fresh install could not get past it.
- **One column, one DEFAULT.** A second `DEFAULT` on the same column is `Msg 1781, Column
  already has a DEFAULT bound to it`, and the batch dies there. `database.sql` carried two
  blocks that both defaulted the same six columns. Related to the rule above: a guard that
  tests `name = 'DF_X_Y'` cannot see a default that was declared unnamed, because SQL Server
  then calls it `DF__X__Y__<hash>` — so an unnamed declaration plus a name-guarded migration
  collides even though both are individually correct.

Both were found by `tests/verify_e2e.py` executing the files, not by reading them, and
neither is something `check_migration_sql.py` can see: the statements are valid-looking and
parse fine as text. That is the whole argument for §4's scratch database.

---

## 5. Conventions

- Top of `main.py` has `# type: ignore`; functions are module-level, no classes.
- Match the existing style: string `f`-format logging, `with get_connection() as conn`
  blocks, and the `conn is None` early-return guard after each DB open.
- **Do not add an `except` around the `yield` in either `get_connection()`.** Catching there and
  yielding a second time is illegal in a generator: Python replaces the real error with
  `RuntimeError: generator didn't stop after throw()`, hiding the actual message. A
  *connection* failure is already handled by `create_connection()` returning `None` (hence
  the `conn is None` guard); a query failure must propagate with its real type and message,
  so a missing migration reads as pyodbc's "Invalid column/object name 'X'". Functions that
  must tolerate an unapplied migration (e.g. the `ReservationArchive` read in
  `search_availability()`) wrap **their own body** in `try/except`.
  There are **two** of these context managers — `db.get_connection()` and an independent copy
  at `main.py:141`. Only `db`'s is correct. `main`'s catches, logs, and re-yields, so every
  caller in `main.py` loses the real exception type; see §7 for what that costs. **The rule is
  about both.**
- `HotelSettings` values are admin-editable free text: every numeric read goes through
  `_setting_float()` / `_setting_int()`, which fall back to the `config.ini` default on a
  blank or non-numeric value. A typo there must never break check-out.

### Git: commit at the end of every change

**When you finish a change in this repo, commit it.** Do not leave a finished piece of work
uncommitted in the working tree for the next agent — or the next human — to unpick. This
overrides any default about not committing unless asked; on this repository, the standing
instruction *is* the ask.

Granularity is **one commit per coherent change, with the checks green**. Concretely:

1. Finish the whole change — not one file edit of it. A commit should be a thing you could
   describe in a sentence.
2. Run the checks that apply (below; at minimum `py_compile` + the unit tests).
3. `git add` the files you changed, **by name** — never `git add -A` or `git add .`.
4. Commit with a message that says *why*, not *what*. The diff already says what.
5. `git push` when the branch is `main` and the checks passed.

Do not amend or rebase published history, and do not force-push.

**Never commit `config.ini`.** It is ignored by `.gitignore`; do not defeat that with
`git add -f`. Note that the old `V1` tag and the `Update-V2` branch still track a `config.ini`
from when it was uploaded through the GitHub web UI — its values are blank, but do not edit
that file on those refs, because on them it is already tracked.

Two things that make this rule safe rather than reckless:

- **Checks green before committing.** A commit that does not pass the suite is a commit that
  cannot be bisected or reverted cleanly, so "commit early" is not "commit broken".
- **`git add` by name, always.** `git add -A` will happily stage `config.ini`, an `exports/`
  CSV full of guest data, or a council transcript.

If you are mid-task and cannot get the checks green, say so in the commit message rather than
quietly committing a failure.

---

## 6. Verification

```powershell
python -m py_compile main.py db.py reports.py ui.py   # syntax
python -m unittest discover -s tests                          # unit tests
python tests/check_schema_sync.py                             # schema drift
python tests/check_migration_sql.py                           # T-SQL lint
python tests/check_docs_sync.py                               # docs are stale?
python tests/check_applied_migrations.py                      # live DB matches database.sql
python tests/verify_e2e.py                                    # run it, on a throwaway database
```

The first six are read-only and safe to run at any time. **`verify_e2e.py` is the only one
that creates anything**, and it creates a database of its own and drops it again — see §4.
It needs a login with `CREATE DATABASE` authority; it refuses to run if
`[verify] verify_database` resolves to the `[database]` target, and it exits non-zero when
anything it checks fails. **It is currently green** (§7), so treat a non-zero exit as a real
finding rather than as "the harness is broken" — and do not respond by loosening the
assertion that caught it.

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
python main.py                                          # run the app
python main.py --report transactions --format csv       # report via the app
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
019-021 were verified read-only against the catalog. Their T-SQL and the flows they back now
run on every `verify_e2e.py` invocation — but against a *disposable* database, so
`hotelSystem` itself still has not had a booking run through it. See `docs/BOOKING.md` §6.

`tests/verify_e2e.py` closes most of that gap and is the check to run before trusting any of
it. As of 2 October 2026 it establishes, on a disposable database:

- `database.sql` builds all 28 tables from an empty server, with no errors.
- All 25 migrations execute in order against a real server — the first automated execution
  of 019-021's T-SQL ever.
- All 25 migrations execute a **second** time without error, so the re-runnability §4
  requires is now measured rather than asserted.
- The 022 backfill populates `NightlyRate` on a row that genuinely lacked one (it had never
  been run against a row before), and the captured-rate read path reads a real non-NULL
  value.
- `database.sql` matches the live catalog column-for-column, apart from the one documented
  `GuestRequests` difference.

- `post_room_charge()` charges the room exactly once per stay, verified by calling it twice.
  (This was a bug until 2 October 2026 — see the note below.)
- `record_booking_payment()` raises `BookingRefTaken` when the booking-reference unique index
  rejects a duplicate, verified on the `conn=` path both production callers use.
  (A misdiagnosis of this path was itself a bug until 3 October 2026 — see below.)
- `main.get_connection` **is** `db.get_connection`, and a failing statement escapes as a
  `pyodbc.Error` carrying the server's own message. These two are regression guards for §7:
  a context manager that swallows every error still hands out usable connections, so
  nothing else in the harness would notice the duplicate returning.
- The loyalty report at all three scopes, read back out of its CSV. The room-scoped one
  asserts 019's actual claim — a statement about one room reports that **guest's** whole
  history — which needs a ledger row in a second room to be distinguishable from a
  room-filtered query at all.
- **Settlement resumability** (Phase 5b): a failure injected at each of `check_out()`'s five
  steps, then an ordinary re-run, must reach exactly the state an uninterrupted check-out
  reaches. All five steps already satisfy this; the point is that it is now measured. Do
  **not** "fix" this by making check-out atomic — settlement prompts for card details, and
  the room charge posts before payment *so a declined card can be retried*. See
  `docs/BOOKING.md` §6.
- **Redemption safety** (Phase 5c): a declined card leaves the guest's balance and loyalty
  ledger untouched. Redemption used to commit before the card prompt, so a declined card
  burned the points with nothing billed, and it was the only loyalty mutation with no
  `SourceID`, so the loss was untraceable. The deduction now runs inside the invoice's
  transaction, keyed `redeem:{invoice_id}`.
- **Rollback safety** (Phase 5d): connections are autocommit-off, an exception inside a
  `with get_connection()` block discards the uncommitted write, and a committed write
  survives the close. The autocommit assertion is first because it is the precondition for
  the other two.
- **Outstanding-settlement signalling** (Phase 5e): a failure at any of `check_out()`'s five
  steps must print what is left undone, name each item, and go quiet once a retry completes.
  Both directions are asserted — the "goes quiet" half matters more, because a signal that
  fires on healthy rooms teaches staff to ignore it. Asserting only the predicate was not
  enough: a version that computes the answer and never prints it passed every check until
  the console output itself was captured.

Two rules for anyone extending Phase 5b, both learned the hard way:

- **The injector sits outside each helper, never inside it.** Five of the six settlement
  helpers have their own `except Exception`; a failure raised from *inside* one would be
  swallowed by that same helper and the step would report as successful.
- **Every scenario needs both vacuity guards** — that the injection actually fired, and that
  the interrupted state differs from the finished one. Without them a step that had quietly
  stopped being called reports a pass. The second guard already earned its place: on its
  first run the prompt router refused to proceed over an unrecognised prompt
  (`Do you have a discount code?`) rather than silently mis-answering it.

As of 4 October 2026 it is **green**, and has been run to completion more than once. The
Phase 5b assertions were proven able to fail — restoring the exact `post_room_charge()` guard
mismatch took four of the five scenarios red, reporting `room charges=2` and
`invoiced total=502.85` against an uninterrupted `276.85`.

### The `main.get_connection()` defect — fixed 3 October 2026

`main.py` used to carry a second copy of the context manager that wrapped its `yield` in
`except Exception` and then yielded again. That is illegal in a generator, so every error
raised inside a `with get_connection()` block reached the caller as
`RuntimeError: generator didn't stop after throw()` instead of pyodbc's message.
`db.get_connection()` had the correct shape all along. `main.get_connection` is now a
plain alias for it (`main.py:141`), so there is one definition, and it is the right one.

**What this never was: a broken booking desk.** An earlier version of this section claimed
`BookingRefTaken` "never fires" because the unique-index violation was swallowed. It did
fire. Both callers of `record_booking_payment()` pass `conn=` — `_write_booking_charge()`
(main.py:1736) and `cancel_booking()` (main.py:2757) — so they take the branch that opens
no connection of its own and never reached the broken copy. The claim came from a
`verify_e2e.py` assertion that called `record_booking_payment()` *without* `conn`, a path no
code in `main.py` takes, and read the resulting `None` as a broken production path. The
assertion now exercises the real path and passes.

The risk that was priced into "this is not mechanical" was also overstated: of 141
`with get_connection()` blocks, 99 are inside a `try` that catches the failure either way,
and the 42 that propagate were **already** killing the console — just with a useless
message. Deleting the duplicate added no crash site. Verified by fault injection: a bad
statement now escapes as `pyodbc.ProgrammingError` with the server's own text, and a failed
*connect* still yields `None`, so the `conn is None` contract is unchanged.

Both probes are now assertions in `verify_e2e.py` Phase 5, and they were checked against a
deliberately restored copy of the broken context manager before being trusted: with it back,
`verify_e2e.py` exits non-zero on both. An assertion that has never been seen to fail is not
evidence, and this repo has already shipped one that was wrong for exactly that reason.

Two rules that survive this fix, because both still look like cleanups and are not:

- **The 130 `conn is None` guards stay.** `create_connection()` returns `None` when a
  **connect** fails, and that is the only path to them. They are not there for query
  failures. Adding guards to more blocks would be dead code; a failed query never yields
  `None`, it raises.
- **`main()`'s menu loop still must not get a top-level exception handler** without its own
  audit. A handler that returns to the menu turns today's fail-stop into a **retryable
  half-written folio** — the `post_room_charge` class of bug. That is a rollback-safety
  question, not an error-handling one. See the audit below: the per-function atomicity
  story turned out to be sound, so this rule rests on *inter-procedure* sequencing.
  Phase 5e has since made the residue **visible** rather than prevented, which is a
  compensating control and not a fix — so the rule survives, and the thing it was worried
  about is at least now reported instead of silent.

### Rollback safety — audited 4 October 2026, and the standing worry was overstated

This file previously said rollback safety was "untouched and unaudited", citing 78
`conn.commit()` sites and no `ROLLBACK` anywhere. That was a guess presented as a finding.
It has now been measured, and the guess was wrong in a way that matters: **most of those 78
sites are already atomic**, and the number was never the thing to worry about.

What the audit actually found, by walking the AST of `main.py` for functions that commit in
more than one `with get_connection()` block:

| | |
|---|---|
| commit sites | 78, across 61 functions |
| functions committing in **2+ separate blocks** | **5** |
| existing explicit rollbacks | **5** (in the booking refund retry loop, `main.py:2555-2583`, `2804`) — not zero, as previously stated |
| functions where a block commits mid-sequence | **0** |

All five multi-block functions are correct **by design**, and the distinction matters:

- Three (`admin_manage_loyalty_tiers`, `manage_amenities_menu`, `manage_promotions_menu`)
  are `while True:` menu loops whose commits sit in **mutually exclusive branches**. Only
  one can ever run, so there is no sequence to be partial about. An AST count cannot see
  this; only reading the function can.
- Two (`advance_order`, `_concierge_inbox`) commit the primary effect, then make a
  secondary effect — an in-room `Notifications` row — inside its own `try/except: pass`.
  That is deliberate: a notification failure must not roll back the status change.

A single `with get_connection()` block whose executes all precede one commit was already
atomic, because an exception exits the block with nothing committed. That is the shape ~56 of
the 61 functions are in, which is why the raw commit count was misleading.

What was genuinely missing was that the discard guarantee was **inherited from the driver**
rather than stated, and nothing tested it. `db.get_connection()` now rolls back explicitly on
the way out — not a behaviour change, since closing an autocommit-off
connection already discards, but it stops anyone relying on that implicitly. It cannot
defend against autocommit, where every statement is durable as issued, so Phase 5d asserts
`autocommit` is off, that an exception discards an uncommitted write, and that a committed
write survives the close.

**What remains genuinely unaudited** is inter-procedure sequencing, not per-function
atomicity: `check_out()` is five separately-committing steps, and resumability is now
measured (Phase 5b) but only for the failures the harness injects, not for a crash between
two processes or a power loss mid-commit.

`post_room_charge()` had the same class of bug and is fixed: its once-only guard selected on
`f"Room charge for {check_in}"` while the `INSERT` wrote `f"{marker} - {room_type}"`, so the
guard could never match its own row and a retried check-out double-charged the guest. The
description is now composed once and used by both sites. `verify_e2e.py` asserts it, so that
cannot regress silently.

`database.sql` was audited against the live catalog in October 2026 and the two agree
on every table, column, type, nullability, primary key, UNIQUE index, CHECK constraint,
DEFAULT constraint, foreign key and secondary index — with one deliberate exception:
`GuestRequests` exists in `database.sql` and not on the live database, because it is the
dead table §3 says not to wire up. That audit fixed a `Transactions.ChargeGroup` declared
`nvarchar(10) NULL` against a real `varchar(10) NOT NULL DEFAULT ('F&B')`, a missing
`Transactions.Description`, six absent column defaults and thirteen absent indexes. The
corrections went into `database.sql` **only**, because the live database already had all of
them and no migration was needed; `database.sql` and `migrations/` therefore differ on
CHECK/DEFAULT/index surface from here on, and `database.sql` is the reference for fresh
installs.

**That audit's conclusion was wrong, and the way it was wrong is the lesson.** A catalog
comparison checks that an object *exists* and has the right shape. It cannot check that the
statement which creates it *parses*, and it cannot check that the file applies in order. So
the October audit passed a `database.sql` that could not build a database at all: a filtered
index written with `OR` in its `WHERE` (`Msg 156`), and a second block re-defaulting six
columns the file had already defaulted (`Msg 1781`). A third defect sat behind them — two
defaults declared unnamed where `migrations/003` adds the same two under names and guards on
those names, so the guard missed and collided. All three were invisible to the comparison and
to `check_migration_sql.py`, and all three are now fixed in `database.sql` (no migration
needed; the live database already had the correct form of each). `verify_e2e.py` Phase 1 and
Phase 4 are what catch this class, which is why they exist rather than a fourth static check.
