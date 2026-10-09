# Hotel Management

A hotel management console: reservations, room inventory and housekeeping, a guest folio with
a split room/F&B bill, a public booking desk with deposits and refunds, and a per-night loyalty
programme. Python 3 on the console, SQL Server behind it.

> **This is a teaching project.** It is deliberately not hardened — see
> [Not production software](#not-production-software) below, and read that before reusing any
> of it.

## What it does

| Area | What you get |
|---|---|
| **Reservations** | Book, edit, search, availability, arrivals/departures board, re-letting |
| **Rooms** | 138k-room inventory across a 150-floor tower, housekeeping status, floor-based views |
| **Billing** | Per-night rates, a folio that splits into Room Charges and F&B, tax, discounts, split folios, invoices |
| **Booking desk** | Public-facing guest accounts, deposits, prepayments, refunds, booking references |
| **Loyalty** | Per-night points accrual, tiers, redemption, statements |
| **Operations** | Door access, valet parking, IT panel, concierge requests, staff alerts |
| **Reports** | 9 CSV exports — transactions, reservations, loyalty, invoices, revenue, occupancy, housekeeping, audit, guest satisfaction |
| **Clearance cards** | Greek-letter clearance/keycard desk: tier × room-category matrix for guests, reserved cards for staff roles, tkinter "tap your card" window, name extracted from the SVG assets |

## Requirements

- Python 3.10+ (developed against 3.14)
- SQL Server (LocalDB, Express, or full)
- `pip install -r requirements.txt` — `pyodbc`, `rich`, `tqdm`, `svglib`, `reportlab`, `rlPyCairo`, `Pillow`, plus the web/API stack `fastapi`, `uvicorn`, `python-multipart`, `itsdangerous` (`httpx` is for the API tests)

## Getting started

```powershell
# 1. Create the database and schema. This is the authoritative fresh-install script:
#    it creates the database if absent, then all 28 tables, constraints, indexes and seeds.
#    Run it in SSMS or sqlcmd. Never run it against a database that already has data.
sqlcmd -S localhost -E -i database.sql

# 2. Configure. config.ini is untracked and holds the SQL Server password.
copy config.ini.example config.ini

# 3. Run it.
pip install -r requirements.txt
python main.py

# 4. Optional: the web/API edition, a second entry point over the same service layer.
#    The console above keeps working unchanged.
uvicorn api:app --reload
```

On first run the app detects that setup has not happened and walks you through it: it creates
your first administrator login, then offers a starter item catalogue (or lets you type your
own), a room layout, and a `master` override account. It runs once. After
that, **Admin Panel → Setup & Destructive → Setup Checklist** shows what is still outstanding and can finish the
job.

Staff-first-run instructions live in **[docs/ONBOARDING.md](docs/ONBOARDING.md)**.

### Upgrading an existing database

Do not re-run `database.sql`. Apply the numbered files in `migrations/` in order — 25 of them,
for databases that predate a feature.

### Reports from the command line

```powershell
python main.py --report transactions --format csv
python reports.py --report loyalty --room 9012
```

## Tests

`pytest` is not used; the suite is stdlib `unittest` and needs no database — a fake cursor
records every statement so a bad column name or a missing `?` fails in the test rather than
against your data.

```powershell
python -m unittest discover -s tests        # 360 tests
```

The static and live checks are separate, and each exits non-zero on failure:

```powershell
python tests/check_schema_sync.py           # database.sql vs migrations/ drift
python tests/check_migration_sql.py         # T-SQL lint
python tests/check_docs_sync.py             # are the docs stale?
python tests/check_applied_migrations.py    # the live server vs database.sql
```

## How it is laid out

| File | Role |
|---|---|
| `main.py` | Entry point and compatibility facade; the app body is split by domain across `core.py`, `session.py`, `rooms.py`, `items.py`, `loyalty.py`, `payments.py`, `keycards.py`, `reservations.py`, `booking_ledger.py`, `billing.py`, `orders.py`, `notifications.py`, `bookings.py`, `customer.py`, `concierge.py`, `admin.py`, `onboarding.py` |
| `ui.py` | `rich` console helpers — menus, tables, prompts |
| `reports.py` | CSV exports, also a standalone CLI |
| `db.py` | Connection string and the `get_connection()` context manager |
| `api.py` | FastAPI web/API edition — a second entry point (`uvicorn api:app`) over the same services as the console. Scaffold only today; see `PLAN-web-api.md` |
| `clearance.py` / `clearance_ui.py` | Clearance-card catalog/mapping and the tkinter keycard window |
| `database.sql` | Authoritative fresh-install script |
| `migrations/` | 25 incremental changes, applied in order |
| `docs/` | Schema reference, booking/loyalty rules, deviations, onboarding |
| `tests/` | The suite, plus the four check scripts |
| `config.ini.example` | The tracked config template. `config.ini` itself is never committed |

`AGENTS.md` is the working agreement for coding agents on this repo: the invariants that must
not be "fixed", the schema rules, and the verification commands.

## Not production software

Read this before taking any of it anywhere real.

- **Passwords are stored and compared in plaintext.** `Users.Password`,
  `CustomerProfiles.Password`, and the password in `config.ini` `[database]`. This is
  deliberate, for teaching, and documented in `AGENTS.md` §3. Do not build on it.
- **There is no real email or SMS.** `Notifications.Channel` is a label; delivery is in-app.
  "Open door" and "test card at reader" stand in for hardware.
- **The first-run setup wizard runs without authenticating anyone**, because on an empty
  database there is no account to authenticate against. It is bounded by two guards: it never
  runs again once complete, and it refuses to create an account if any already exists.
- **`database.sql` and `migrations/` are kept in sync by hand**, and the sync checker compares
  columns, primary keys and UNIQUE indexes only — not CHECK, DEFAULT or non-UNIQUE indexes.
  That gap has bitten before; `AGENTS.md` §4 explains it.
- **Never commit `config.ini`.** It holds a live database password.

## Documentation

| Document | Covers |
|---|---|
| [docs/SCHEMA.md](docs/SCHEMA.md) | Every table, the migration inventory, the degradation matrix |
| [docs/BOOKING.md](docs/BOOKING.md) | Booking money, deposits, refunds, loyalty, guest identity and privacy |
| [docs/DEVIATIONS.md](docs/DEVIATIONS.md) | What this app deliberately does *not* do, and what would close each gap |
| [docs/ONBOARDING.md](docs/ONBOARDING.md) | Staff first-run order |
| [AGENTS.md](AGENTS.md) | Invariants, schema rules, conventions, verification |

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

AGPL rather than GPL because this is a server-side application: if you run a modified version
over a network, section 13 requires you to offer those users the corresponding source.