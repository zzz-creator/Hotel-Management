# Plan: Split the UI out of `main.py` — one business core, two front ends

Status: **in progress.** Phase 0 begun (`main-backup.py` copied, byte-identical).

## Goal

Make `main.py` a business core with **no user interface in it at all** — no `input()`, no
`ui.*`, no `logging.info`. Then build two front ends over that one core:

- `main-console.py` — today's `rich` console UX, unchanged in behaviour.
- `main-tkinter.py` — a new tkinter GUI application.

`main-backup.py` stays in the repository **unchanged and permanently**, as the pre-split
code, so the two implementations can be compared line by line without rewriting either.
It is a reference copy, not a fallback.

## Why one core and two front ends

The alternative — copy `main.py` to `main-tkinter.py` and rewrite the UI in place — was
considered and rejected on measurement. Of the 236 functions in `main.py`:

| Bucket | Count |
|---|---|
| Database access **and** prompts in one body | 52 |
| Database only | 70 |
| I/O only | 44 |
| Pure | 70 |

Duplicating the file duplicates all the money and loyalty logic into a file **no test
touches**. Under one core there is exactly one `bill_room_transactions()`, and both front
ends call it.

## What the split costs, measured not assumed

**The test suite is a thinner net than it looks.** Only **69 of 236** functions are called
by `tests/` at all; 167 are never invoked. Coverage concentrates in the pure billing and
loyalty arithmetic, which lands in the core regardless of how the split is done. So "the
tests are green" is a weaker safety net for this work than its reputation suggests, and
the risk concentrates in the split itself.

**Five functions are tested through mocked `input()`** and will need their tests rewritten,
not merely kept running:

| Function | `app.<fn>()` call sites in `tests/` |
|---|---|
| `customer_login` | 8 |
| `add_reservation` | 2 |
| `add_custom_item` | 2 |
| `validate_room` | 1 |
| `cancel_booking` | 1 |

Those five are exactly the ones whose signatures change when prompts are lifted out, so
the rewrite is unavoidable and lands in Phase 0, in the same commit as the change.

## The rule that has to be machine-checked

The core/front-end boundary is only real if something fails when it is crossed. A new
lint, `tests/check_ui_separation.py`, fails if `main.py` contains `input(`, `ui.`, or
`logging.info`.

This mirrors how AGENTS.md §7 already machine-checks the duplicate `get_connection()` that
once lived in `main.py`. Same argument: an unenforced convention is a convention one
`input(` away from gone. The lint ships **with** the phase that makes it pass, never before.

AGENTS.md §5's "new interactive output goes through `ui.py`" is amended to **one UI module
per front end** — `ui.py` for `rich`, `ui_tk.py` for tkinter. The intent (no inline widget
code, helpers live in a module) is kept; only the module name changes.

## Phases

Each phase ends runnable, and each is one commit with the checks green.

| Phase | Work | Gate |
|---|---|---|
| 0 | Split the core out of `main.py` | 345 tests green, new lint green |
| 1 | `ui_tk.py` primitives | imports clean, widgets build headlessly |
| 2 | `main-console.py` rebuilds today's UX from the core | console app works as before |
| 3 | Tk shell: login, main menu, first-run onboarding | wizard runs once, refuses if accounts exist |
| 4 | Front-desk flows: booking, check-in/out, rooms, billing | declined card still retryable |
| 5 | Remaining menus: loyalty, admin, reports, IT/valet/door | feature parity with console |
| 6 | Docs and checkers | `check_docs_sync.py` green |

**Phase 0 — split the core.** Extract the 52 fused functions into `core(inputs) -> result`
pairs plus explicit prompt points. Move the 44 I/O-only functions to the front ends. The
70 pure and 70 DB-only functions move unchanged. Rewrite the five mocked-`input` tests in
the same commit.

**Phase 2 before Phase 3 is deliberate.** Rebuilding the console front end is the cheap
proof that the core split did not break behaviour — exercised against 345 tests through an
interface that already works — before any GUI exists. Without it, the first real test of
the split happens through a new GUI, where a regression is far harder to localise. Skipping
Phase 2 is a legitimate shortcut, but say so before skipping it.

## Invariants the split must not disturb

Each of these is load-bearing and has already caused a bug once. They are stated here
because a rewrite is exactly the operation that silently breaks them.

- **Check-out ordering.** `check_out()` is five separately-committing steps:
  `post_room_charge` → `bill_room_transactions` → `set_room_status` →
  `revoke_active_key_cards` → `award_stay_points`, and the card prompt sits *between* them.
  `docs/DEVIATIONS.md` §10 is emphatic that this order is load-bearing: a declined card must
  stay retryable, so the room charge posts before payment and the room does not flip to
  `Dirty` until money is settled. A tkinter dialog that reorders these introduces a money
  bug into a working app. **Do not make settlement atomic.**
- **Redemption.** `redeem_points_for_invoice()` deducts inside the invoice transaction, keyed
  `redeem:{invoice_id}`. A declined card must leave the balance and the ledger untouched
  (`docs/DEVIATIONS.md` §9). The Tk card prompt cannot move inside the transaction.
- **Card prompts never raise.** `luhn_check()` returns `False` for a non-digit run;
  `validate_expiration_date()` rejects a month outside 1..12. A mistyped card must produce a
  message, not a traceback. `LAST_CARD_DIGITS` clears on every rejection.
- **Guest identity.** `validate_room()` stays last name + FIRST name + room number in one
  query, bounded to three attempts, never listing other stays for a surname
  (`docs/BOOKING.md` §1). This is a privacy boundary, not a lookup convenience.
- **Loyalty is keyed on `CustomerID`,** never on room number, or a balance is inherited by
  whoever checks into that room next.
- **First-run wizard trust assumption.** It runs unauthenticated because an empty database
  has no account to authenticate against. Both guards are non-negotiable:
  `create_first_user()` refuses if *any* account exists, and the wizard never re-runs once
  `mark_onboarding_complete()` has run. No "re-run setup" shortcut on the main menu.
- **`get_connection()` is `db.get_connection`,** an alias. Never a second definition. Never
  wrap the `yield` in `except`.
- **The room charge is posted by the app,** as a `Transactions` row with `ItemID` NULL and
  `ChargeGroup` `'Room'`. There is deliberately no "Room Charge" item in `Items`.

## Not in scope

- Any schema change. No migration, no `database.sql` edit.
- Password hashing. Plaintext is intentional (`docs/DEVIATIONS.md` §8).
- A night audit or snapshot table (`docs/DEVIATIONS.md` §3).
- Web or multi-user. Tkinter is a single process, which is what the test harness assumes.

## Verification

Unchanged from AGENTS.md §6, plus the new lint:

```powershell
python -m py_compile main.py main-backup.py main-console.py main-tkinter.py ui.py db.py reports.py ui_tk.py
python -m unittest discover -s tests
python tests/check_ui_separation.py          # new: core has no UI in it
python tests/check_schema_sync.py
python tests/check_migration_sql.py
python tests/check_docs_sync.py
python tests/verify_e2e.py                   # creates and drops a disposable database
```