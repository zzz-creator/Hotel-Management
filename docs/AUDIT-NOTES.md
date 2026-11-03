# Audit notes moved from AGENTS.md

### The `main.get_connection()` defect — fixed 3 October 2026

`main.py` used to carry a second copy of the context manager that wrapped its `yield` in
`except Exception` and then yielded again. That is illegal in a generator, so every error
raised inside a `with get_connection()` block reached the caller as
`RuntimeError: generator didn't stop after throw()` instead of pyodbc's message.
`db.get_connection()` had the correct shape all along. `main.get_connection` is now a
plain alias for it, so there is one definition, and it is the right one.

**What this never was: a broken booking desk.** An earlier version of this section claimed
`BookingRefTaken` "never fires" because the unique-index violation was swallowed. It did
fire. Both callers of `record_booking_payment()` pass `conn=` — `_write_booking_charge()`
(in `booking_ledger.py`) and `cancel_booking()` (in `bookings.py`) — so they take the branch that opens
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
| existing explicit rollbacks | **5** (in the booking refund retry loop inside `cancel_booking()`) — not zero, as previously stated |
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
