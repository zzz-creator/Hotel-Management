# Concurrency plan: multiple staff at separate terminals

Status (as of 2 Nov 2026): **Phase 1 complete** (1A-1G). **Phase 2 mostly complete**
(2A, 2B, 2C, 2D done; 2E stress harness extended with `--phase` flag). Migrations
030-033 applied to the live database.

Approved design. Scope: **DB-level races only** — multiple staff running
separate app instances against one SQL Server database. No shared-process UI
refactor, no web layer. The console-per-staff model stays; the database
becomes the concurrency boundary.

## Decisions

| Question | Decision |
|---|---|
| Deployment model | Separate terminals / instances against one DB (no shared process) |
| Isolation | `SERIALIZABLE` on claim rows via `UPDLOCK, HOLDLOCK`; read-committed elsewhere |
| ItemID generation | Move `Items.ItemID` to `IDENTITY(1,1)`; drop the manual `add_item()` ID prompt |
| Phase 1 delivery | Booking + check-in + check-out first, with stress harness; F&B/loyalty/refunds/keycards in phase 2 |
| Audit attribution | Pass acting user explicitly down the call chain (no `session.CURRENT_USER` reads inside helpers) |

## Architecture

- **Claim-based idempotency, not distributed locks.** Each workflow claims the
  row it is about to change with a conditional `UPDATE ... WHERE <guard> = 0`
  and checks `rowcount == 1`. A lost race gets `rowcount == 0` and the helper
  returns "already done" instead of erroring or double-writing.
- **Per-step commits stay.** `check_out()` remains five separately-committing
  steps (card retry, resumability — see `docs/BOOKING.md` §6). Concurrency
  makes each step idempotent, not atomic.
- **Isolation is opt-in.** `get_connection()` defaults to read-committed; a
  workflow that needs a claim passes `isolation="serializable"` and uses the
  `claim_row()` helper. This keeps the 99 read-only blocks fast.
- **Audit attribution is explicit.** `log_audit(..., user=...)` with default
  `'system'`; `admin_login()` passes the signed-in username through. No module
  reads `session.CURRENT_USER` at audit time.

## Schema changes (one migration + `database.sql` mirror)

Numbered one past the current highest (029). Call it `030_concurrency_claims.sql`.

1. `Items.ItemID` → `IDENTITY(1,1)`. Migration must preserve existing rows
   (recreate table or `SET IDENTITY_INSERT` during copy). Fresh install gets it
   from `database.sql`.
2. `Reservations` + `RoomChargePosted BIT NOT NULL DEFAULT 0` (claim for
   `post_room_charge()`).
3. `Reservations` + `SettlementStatus NVARCHAR(20) NULL` (tracks which of the
   five check-out steps have landed; NULL = not started).
4. `Rooms` already has `Status`; no new column, but check-in gains a conditional
   `UPDATE ... WHERE Status = 'Available'`.
5. `ReservationPayments` already has `AppliedAmount` (migration 020); refunds
   gain a claim on it (phase 2).

`tests/check_schema_sync.py` must be extended to cover the new columns, and
`tests/verify_e2e.py` must exercise the migration against a populated `Items`
table (IDENTITY conversion is the risky part).

## Phase 1 — booking + check-in + check-out (≈3 weeks)

### 1A. DB layer (≈4 days)

Files: `hotel/db.py`

- `get_connection(isolation="read_committed")` — set
  `conn.autocommit = False` (already) and, when serializable, issue
  `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` on connect.
- `claim_row(cursor, sql, params)` helper: executes `sql` (caller supplies the
  `WITH (UPDLOCK, HOLDLOCK)` hint) and returns `cursor.rowcount`.
- Unit-test the isolation level is actually set (new test in `tests/test_db.py`
  or `verify_e2e.py`).

### 1B. Items IDENTITY (≈2 days)

Files: `hotel/items.py`, `hotel/onboarding.py`, `tests/test_onboarding.py`,
`migrations/030_*.sql`, `database.sql`

- `add_custom_item()`: drop `MAX(ItemID)+1` query; `INSERT ... (Name, Price, PricingRule)` and read `@@IDENTITY`/`SCOPE_IDENTITY()`.
- `add_item()`: remove the typed-ID prompt (or keep it as a "suggested ID" that
  is ignored). Update the Admin Panel menu text and `docs/ONBOARDING.md` §3.
- `seed_default_items()`: drop the `MAX(ItemID)` lookup; insert and collect
  assigned IDs.
- Update `test_onboarding.py` ItemID assertions to check "no collision, contiguous
  from 1 on empty table, existing rows untouched".

### 1C. Claim-based booking (≈4 days)

Files: `hotel/reservations.py`, `hotel/booking_ledger.py`

- `book_reservation()` (or equivalent): open a serializable connection, check
  availability with `WITH (HOLDLOCK)` on the overlapping reservation rows, insert
  if free. Return a typed result (`Booked` / `RoomTaken` / `Unavailable`) rather
  than a boolean.
- Re-letting path: same claim pattern.
- Booking reference: already unique-indexed; keep as-is.

### 1D. Claim-based check-in (≈2 days)

Files: `hotel/reservations.py`, `hotel/rooms.py`

- Check-in claims the room: `UPDATE Rooms SET Status='Occupied' WHERE RoomNumber=? AND Status='Available'`.
  `rowcount == 0` → "room no longer available, pick another".
- Reservation status flip (Confirmed → CheckedIn) in the same transaction.

### 1E. Claim-based check-out (≈4 days)

Files: `hotel/billing.py`

- `post_room_charge()`: replace the description-string guard with
  `UPDATE Reservations SET RoomChargePosted=1 WHERE ReservationID=? AND RoomChargePosted=0`;
  insert the charge only if `rowcount == 1`; on `0`, log "already posted" and return
  the existing charge.
- Invoice finalization: claim on `Invoices.InvoiceID` (already PK); write the
  folio rows idempotently keyed by `(InvoiceID, ChargeGroup)`.
- Loyalty accrual: `SourceID = 'stay:{reservation_id}'` (already pattern from
  redemption); skip if a row with that `SourceID` exists.
- `SettlementStatus` column tracks progress so a re-run skips landed steps.

### 1F. Audit attribution (≈2 days)

Files: `hotel/core.py` (`log_audit`), all modules calling it

- Add `user=None` parameter; default `'system'`.
- Replace `session.CURRENT_USER` reads at audit sites with the passed value.
- `admin_login()` passes the username into the admin menu context; menus pass
  it down to the action functions (or store it in a contextvar for the duration
  of the menu loop, which is simpler and keeps signatures stable).

### 1G. Stress harness (≈2 days)

Files: `tests/stress_concurrency.py` (new)

- Scratch-DB setup reused from `verify_e2e.py`.
- N worker threads (default 8) running randomized sequences of
  book/check-in/check-out against distinct and overlapping room/date combos.
- Invariants asserted after the run:
  - No room has two overlapping confirmed reservations.
  - No stay has more than one `Room`-group charge row.
  - Every checked-out stay has exactly one invoice with matching folio totals.
  - Loyalty ledger has at most one accrual per stay.
- Run as a script (`python tests/stress_concurrency.py`), not part of the default
  unittest suite (too slow), but runnable in CI.

**Phase 1 exit criteria:** stress harness green on 8 workers × 200 operations;
`verify_e2e.py` still green; all existing tests green.

---

## Phase 2 — F&B, loyalty, refunds, keycards (≈4–6 weeks)

- `hotel/orders.py` — claim the order before posting its F&B charge; prevent
  double-billing of the same order.
- `hotel/loyalty.py` — accrual idempotency via `SourceID = 'order:{order_id}'`;
  redemption already keyed `redeem:{invoice_id}`.
- `hotel/booking_ledger.py` — refunds claim `AppliedAmount` with a rowcount check
  before writing the refund row.
- `hotel/keycards.py` — card issuance/revocation claim the card row.
- Extend stress harness to cover these workflows.

## Risks

1. **Lock escalation.** `SERIALIZABLE` ranges can escalate to table locks under
   load. The stress harness must measure contention; if it bites, narrow the
   locked range or switch hot paths to `READ COMMITTED` + optimistic version
   columns.
2. **IDENTITY migration on populated tables.** Recreating `Items` with IDENTITY
   while preserving rows and foreign-key references (none today, but check) is
   the riskiest DDL in this plan. `verify_e2e.py` must exercise it before it
   ships.
3. **Resumability vs. concurrency.** A crash mid-settlement leaves a half-done
   folio by design. Claim columns make re-runs safe but do not make settlement
   atomic — that is intentional and documented (`docs/BOOKING.md` §6).
4. **Audit attribution breadth.** Phase 1F touches most modules. If scope slips,
   it can be deferred: keep `session.CURRENT_USER` reads and only pass the user
   where a helper is newly written. Decide before 1F starts.

## Open items (decide before starting)

- [x] Should `add_item()`'s manual-numbering path be removed entirely, or kept
      as "suggest an ID" that the DB overrides? (Affects menu text + docs only.)
      → Resolved: removed (IDENTITY now assigns the id).
- [ ] Phase 1F: explicit user param everywhere, or contextvar for the menu loop?
      → Deferred; `log_audit(user=...)` seam exists, default reads `session.CURRENT_USER`.
- [x] Stress harness worker count and operation mix — needs a baseline run to
      tune. → Baseline run: 8 workers × 200 ops, 0 violations (Phase 1).

## Files touched (phase 1)

| File | Change |
|---|---|
| `hotel/db.py` | isolation param, `claim_row()` helper |
| `hotel/items.py` | IDENTITY inserts; drop MAX+1 and typed-ID prompt |
| `hotel/onboarding.py` | wizard/checklist item text; `_offer_item_catalogue` wording |
| `hotel/reservations.py` | claimed booking, check-in |
| `hotel/rooms.py` | claimed room status flip |
| `hotel/billing.py` | `RoomChargePosted` claim, `SettlementStatus`, idempotent folio/loyalty |
| `hotel/core.py` | `log_audit(user=...)` |
| all modules | pass acting user to `log_audit` |
| `migrations/030_*.sql` | new migration |
| `database.sql` | IDENTITY + new columns |
| `tests/test_onboarding.py` | ItemID assertions |
| `tests/test_billing_math.py`, `test_booking.py` | new claim invariants |
| `tests/stress_concurrency.py` | new |
| `tests/verify_e2e.py` | migration + stress smoke |
| `docs/SCHEMA.md`, `docs/BOOKING.md`, `docs/ONBOARDING.md` | new columns, new rules |

## Exit criteria (phase 1)

- `verify_e2e.py` green (including the new migration against a populated DB).
- Stress harness green: 8 workers × 200 operations, zero invariant violations.
- All existing unit tests green.
- No regression in the resumability/retry behavior (Phase 5b/5e assertions).

---

## Phase 2 detail: F&B, loyalty, refunds, keycards

Phase 2 assumes Phase 1 is shipped and the stress harness exists. Each subsection
follows the same pattern as Phase 1: identify the claim row, make the write
conditional, make re-runs no-ops, add invariants to the stress harness.

### 2A. F&B orders (`hotel/orders.py`) — ≈1.5 weeks

**The race:** two clerks (or a clerk and a guest kiosk) post the same in-room
order's charge to the folio twice, or an order is billed before its items are
fully entered.

**Claim surface:** `Orders` table (header) + `Transactions` rows with
`ChargeGroup = 'F&B'`.

**Changes:**
- Add `Billed BIT NOT NULL DEFAULT 0` and `BilledAt DATETIME NULL` to `Orders`
  (migration 031).
- `post_order_charge(order_id)`: claim with
  `UPDATE Orders SET Billed=1, BilledAt=GETDATE() WHERE OrderID=? AND Billed=0`.
  Only if `rowcount == 1`: read the order lines, compute the total, insert the
  `Transactions` row with `ChargeGroup='F&B'` and
  `SourceID = 'order:{order_id}'`. On `rowcount == 0`: log "already billed" and
  return the existing charge row.
- `advance_order()` (placed → preparing → delivered → billed): guard each
  transition with
  `UPDATE Orders SET Status=? WHERE OrderID=? AND Status=?` so two clerks can't
  both move an order from "preparing" to "delivered".
- F&B charge posting must happen **after** the order is marked delivered, not at
  placement, to avoid billing cancelled orders. Add a `Status = 'Delivered'`
  precondition to the bill claim.

**Idempotency key:** `SourceID = 'order:{order_id}'` in `Transactions`.

**Tests:**
- Unit: `post_order_charge` twice → one charge row; `advance_order` race → one
  transition.
- Stress: N workers place/deliver/bill overlapping orders; invariant: one F&B
  charge per delivered order, no double.

**Files:** `hotel/orders.py`, `hotel/billing.py`, `migrations/031_*.sql`,
`database.sql`, `tests/test_orders.py` (new or extend).

### 2B. Loyalty accrual & redemption (`hotel/loyalty.py`) — ≈1 week

**The race:** points accrued twice for the same stay/order; redemption deducted
but card declined (already fixed in Phase 5c for single-thread; needs claim
under concurrency).

**Claim surfaces:** `LoyaltyAccounts.Points`, `LoyaltyTransactions` (with
`SourceID`).

**Changes:**
- Accrual (`award_billed_order_points`, stay accrual): claim via
  `INSERT INTO LoyaltyTransactions (..., SourceID) ...` with a unique index on
  `SourceID` (or `INSERT ... WHERE NOT EXISTS`). `SourceID` format:
  `'stay:{reservation_id}'`, `'order:{order_id}'`. On duplicate-key violation,
  swallow and return "already awarded".
- Add a unique index on `LoyaltyTransactions.SourceID` (migration 032) — this
  is the DB-level guard; the Python claim is the fast path.
- Redemption (`redeem_points_for_invoice`): already keyed
  `redeem:{invoice_id}`; add the same unique-index guard so a concurrent
  redemption attempt fails at the DB rather than double-deducting. The existing
  transactional deduction (Phase 5c) stays; the unique index is the safety net.
- Tier recompute (`recompute_tier_by_customer` / `admin_recompute_all_tiers`):
  make it a single
  `UPDATE LoyaltyAccounts SET Tier = ... WHERE CustomerID = ?` based on a
  recomputed lifetime total read in the same transaction; concurrent recomputes
  are idempotent because the result is deterministic.

**Idempotency key:** `SourceID` in `LoyaltyTransactions`.

**Tests:**
- Unit: accrual called twice → one ledger row, points added once.
- Stress: concurrent check-outs of different stays for the same customer;
  invariant: lifetime points = sum of distinct `SourceID` accruals.

**Files:** `hotel/loyalty.py`, `migrations/032_*.sql`, `database.sql`,
`tests/test_customer_loyalty.py`.

### 2C. Refunds & booking ledger (`hotel/booking_ledger.py`, `hotel/bookings.py`) — ≈1.5 weeks

**The race:** two clerks refund the same booking payment, or a partial refund
races with a full cancellation refund, resulting in over-refund.

**Claim surfaces:** `BookingPayments` (with `AppliedAmount` from migration 020),
`Reservations` (refund cutoff).

**Changes:**
- `record_booking_payment()` already has `BookingRefTaken` handling; extend the
  same pattern: claim the payment row with
  `UPDATE BookingPayments SET AppliedAmount = AppliedAmount + ? WHERE PaymentID = ? AND AppliedAmount + ? <= Amount`
  (or a simpler `WHERE RefundedAmount + ? <= Amount`). `rowcount == 0` →
  "refund exceeds remaining balance".
- Add `RefundedAmount DECIMAL(10,2) NOT NULL DEFAULT 0` to `BookingPayments` if
  not present (check schema first; migration 020's `AppliedAmount` may already
  cover this — verify before adding).
- Cancellation refund (`cancel_booking()`): wrap the refund-cutoff check + refund
  write in a serializable transaction; claim the reservation with
  `UPDATE Reservations SET Status='Cancelled' WHERE ReservationID=? AND Status IN ('Confirmed','CheckedIn')`.
  `rowcount == 0` → "already cancelled or checked out".
- Deposit prepayment application: same claim pattern on `BookingPayments`.

**Idempotency key:** `(PaymentID, RefundID)` or a `RefundSourceID` column.

**Tests:**
- Unit: two concurrent refunds of the same payment → one succeeds, one gets
  "exceeds remaining balance".
- Stress: concurrent cancellations of the same booking → one cancellation, one
  "already cancelled".

**Files:** `hotel/booking_ledger.py`, `hotel/bookings.py`, `migrations/033_*.sql`,
`database.sql`, `tests/test_booking.py`.

### 2D. Keycards (`hotel/keycards.py`) — ≈1 week

**The race:** two clerks issue or revoke a card for the same guest
simultaneously; a card tested at the reader races with a revocation.

**Claim surfaces:** `KeyCards` table (card number, status, expiry).

**Changes:**
- Issue: `INSERT INTO KeyCards (...)` with a unique constraint on
  `(ReservationID, CardNumber)` or `(GuestID, Active)`. Claim via
  `INSERT ... WHERE NOT EXISTS (SELECT 1 FROM KeyCards WHERE ReservationID=? AND Active=1)`.
  `rowcount == 0` → "card already issued".
- Revoke: `UPDATE KeyCards SET Status='Revoked', RevokedAt=GETDATE() WHERE CardID=? AND Status='Active'`.
  `rowcount == 0` → "already revoked".
- "Test card at reader": read-only check
  (`Status='Active' AND Expiry >= GETDATE()`); no claim needed, but the result
  must reflect the committed state (read-committed is fine; a card revoked in
  another session won't be readable until commit).
- Lost-card reporting: same conditional update pattern as revoke.

**Idempotency key:** `CardID` (PK) + status precondition.

**Tests:**
- Unit: issue twice → one card; revoke twice → one revocation.
- Stress: concurrent issue/revoke for the same reservation; invariant: at most
  one active card per reservation.

**Files:** `hotel/keycards.py`, `migrations/034_*.sql` (if new columns),
`database.sql`, `tests/test_keycards.py`.

### 2E. Stress harness extension (≈3 days)

Files: `tests/stress_concurrency.py`

- Add workflow mixes: F&B order lifecycle, loyalty accrual/redemption, refund,
  keycard issue/revoke.
- New invariants:
  - One F&B charge per delivered order.
  - Loyalty ledger `SourceID` values are distinct per source.
  - `RefundedAmount <= Amount` for every payment.
  - At most one active keycard per reservation.
- Add a `--phase 1|2|all` flag so phase 1 invariants still run independently.

### Phase 2 exit criteria

- Stress harness green with all workflows mixed: 8 workers × 500 operations.
- All existing unit tests green.
- `verify_e2e.py` green (new migrations against populated DB).
- No regression in Phase 5b/5c/5d/5e assertions.

### Cross-cutting concerns

- **Audit attribution (Phase 1F) carries over.** Every new claim site passes
  the acting user to `log_audit()`. No new `session.CURRENT_USER` reads.
- **Migration ordering.** 030 (phase 1) → 031 (orders) → 032 (loyalty) →
  033 (refunds) → 034 (keycards). Each is re-runnable and guarded.
- **`database.sql` parity.** Every column, index, and constraint added in
  migrations must be mirrored. `check_schema_sync.py` covers columns/PKs/unique
  indexes; CHECK/DEFAULT/non-unique indexes are hand-mirrored (per AGENTS.md §4).
- **Docs.** `docs/SCHEMA.md` (new columns, constraints), `docs/BOOKING.md`
  (refund/loyalty concurrency rules), `docs/ONBOARDING.md` (if any new setup
  steps). Run `check_docs_sync.py`.

### Sequencing within Phase 2

1. 2B (loyalty) — smallest surface, unique-index guard is the bulk of the work.
2. 2A (F&B) — depends on loyalty accrual being idempotent.
3. 2C (refunds) — depends on booking ledger being stable.
4. 2D (keycards) — independent, can run in parallel with 2C.
5. 2E (stress harness) — last, validates everything.

### Effort summary

| Sub-phase | Effort |
|---|---|
| 2A F&B | ≈1.5 weeks |
| 2B Loyalty | ≈1 week |
| 2C Refunds | ≈1.5 weeks |
| 2D Keycards | ≈1 week |
| 2E Stress harness | ≈3 days |
| **Total Phase 2** | **≈5.5 weeks** |

Combined with Phase 1 (≈3 weeks), the full concurrency project is roughly
**8.5 weeks** of focused work.