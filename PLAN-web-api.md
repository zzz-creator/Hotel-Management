# PLAN: Web/API edition alongside the console

**Status:** approved 9 October 2026. Phases 1-4 shipped; phase 5 (expand) is next.

## Goal

Add a **FastAPI** web/API front-end for multi-user use, **without changing the console
edition**. `python main.py` keeps working exactly as it does today; the API is a second,
parallel entry point (`uvicorn api:app`). Both front-ends call **one** non-interactive
service layer, so there is a single implementation of each business rule.

This file is the design of record. It is a plan, not a subsystem that ships.

## Decisions (locked 9 October 2026)

1. **Service functions live in the existing domain modules.** No new `services/` package.
   The interactive console functions become thin adapters that gather input and delegate
   to a non-interactive twin. One definition, so the two front-ends cannot drift
   (`AGENTS.md` §5).
2. **Signed-cookie sessions** (Starlette `SessionMiddleware` + `itsdangerous`). No schema
   change, no migration.
3. **Plaintext passwords stay** (`AGENTS.md` §3). The web edition is scoped to a trusted
   network over TLS and documented as such; no change to the stored form.
4. **Pilot slice:** login → availability → create booking → check-in.

## Why this is mostly a decoupling project

The API surface is cheap. The work is that business logic is welded to console I/O — the
same functions that own the SQL also call `input()` and render with `rich`.

Measured 9 October 2026 (`Select-String`, per module):

| Module | `input()` sites | `ui.*` sites |
|---|---|---|
| `admin.py` | 35 | 39 |
| `items.py` | 28 | 10 |
| `customer.py` | 23 | 15 |
| `orders.py` | 23 | 26 |
| `reservations.py` | 17 | 19 |
| `billing.py` | 16 | 14 |
| `core.py` | 13 | 3 |
| `loyalty.py` | 11 | 11 |
| `onboarding.py` | 7 | 23 |
| `rooms.py` | 7 | 10 |
| `notifications.py` | 7 | 4 |
| `bookings.py` | 6 | 14 |
| `keycards.py` | 6 | 8 |
| `concierge.py` | 4 | 10 |
| `payments.py` | 3 | 0 |
| `main.py` | 1 | 3 |
| `ui.py` | 5 | 0 |
| **Total** | **212** | **209** |

Three measured facts that shape the design:

- **`log_audit()` reads `session.CURRENT_USER` directly** (`core.py:255`). Audit
  attribution is a process global, so it needs an explicit actor parameter on the web path.
- **`session.LAST_CARD_DIGITS` is a process global.** `booking_ledger.py:267` calls it "the
  last card the PROCESS authorised". Under concurrent users, guest A's card digits can be
  attributed to guest B's refund. This **must** be de-globalised before any card-capturing
  endpoint ships (billing phase).
- **`tests/verify_e2e.py:1090-1160` drives `check_out()` by matching prompt text** after
  monkeypatching `builtins.input`. The console's prompt strings are a de-facto test
  contract: adapters must preserve them, or that router must be updated.

## Target architecture

```
            ┌──────────────┐     ┌────────────────────┐
 console →  │  main.py     │     │  api.py (FastAPI)   │  ← new, `uvicorn api:app`
 (UX         │  ui adapter  │     │  routers + schemas  │
  unchanged) └──────┬───────┘     └─────────┬──────────┘
                    │  both call the same ↓  │
             ┌──────┴────────────────────────┴──────┐
             │  service functions: explicit args,    │
             │  return a result / raise domain error, │
             │  take an `actor`, do no I/O            │
             └──────────────────┬────────────────────┘
                                │ db.get_connection() (sync pyodbc)
```

- **Services:** each interactive function gets a non-interactive twin, e.g. `check_out()`
  (prompts) becomes an adapter over `perform_check_out(room, actor, card=..., discount=...,
  redeem=...)`. Same SQL; arguments in, outcome out.
- **Auth/session:** a FastAPI dependency resolves the principal per request from the signed
  cookie. Service functions take the actor explicitly; `log_audit()` gains an explicit
  `user=` argument (console passes `session.CURRENT_USER`, web passes the request principal).
- **Blocking DB:** endpoints are `def` (not `async def`) so Starlette runs them in a
  threadpool; sync pyodbc is then fine. Never `async def` a handler that calls pyodbc
  directly.
- **CSRF:** cookie auth on state-changing methods requires `SameSite=Lax/Strict` (plus a
  token if exposure widens beyond the trusted network).

## Files

**New**

| File | Role |
|---|---|
| `api.py` | `app = FastAPI()`, `SessionMiddleware`, routers, `uvicorn api:app` entry |
| `auth.py` | Credential check (reusing `Users`/`CustomerProfiles`), cookie issue/clear, `current_principal` dependency |
| `schemas.py` | Pydantic request/response models |
| `tests/test_api.py` | `unittest` + Starlette `TestClient` |

**Modified**

| File | Change |
|---|---|
| `rooms.py` / `reservations.py` | Non-interactive availability + check-in services; console delegates |
| `bookings.py` | Create-booking service |
| `admin.py` / `customer.py` | Login services |
| `core.py` | `log_audit(..., user=None)` actor parameter |
| `requirements.txt` | `fastapi`, `uvicorn`, `python-multipart`, `itsdangerous`, `httpx` (test) |
| `README.md`, `AGENTS.md` §2 | New files in the file map (`check_docs_sync.py`) |

**Pilot endpoints:** `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/me`,
`GET /api/rooms/availability`, `POST /api/bookings`, `POST /api/reservations/{room}/check-in`.

## Phased plan

Each phase is one coherent, signed commit with the checks green.

- **Phase 1 — scaffold.** `api.py` skeleton, `requirements.txt`, settings reused from
  `core`. No behaviour change to the console.
- **Phase 2 — service extraction for the pilot slice.** Make `log_audit` take an actor
  (commit 1). Then extract services for login, availability, create-booking and check-in in
  the owning modules; the console functions delegate; prompt text preserved so
  `verify_e2e.py` stays green.
- **Phase 3 — auth + session.** Cookie issue/verify, per-request principal, actor plumbed to
  `log_audit`, `LAST_CARD_DIGITS` de-globalised on the web path.
- **Phase 4 — pilot endpoints.** Pydantic models, error mapping, OpenAPI docs.
- **Phase 5 — expand.** `rooms`, `reservations`, `billing`/folios, `loyalty`, `orders`,
  `admin`; `reports.py`'s `REPORTS` registry maps almost 1:1 to read-only endpoints.
- **Phase 6 — tests + docs.** `unittest`-wrapped `TestClient`; README/AGENTS file map.

## Progress / TODO

- [x] **Commit 1 — `log_audit()` actor parameter.** Additive; console unaffected; existing
      audit tests stay green; new test pins the override.
- [x] Phase 1 — scaffold `api.py`, `requirements.txt`
- [x] Phase 2 — extract services (login, availability, create booking, check-in); console delegates
- [x] Phase 3 — auth + session (cookies, principal, actor plumbing, de-globalise card digits)
      Shipped dependency-light: `auth.py` has no FastAPI/Starlette import, so it imports
      before the web deps are installed. `current_principal` is a plain callable over the
      signed session dict; phase 4 registers it with `Depends()` and owns the Request
      annotation, the `SameSite` baseline and the HTTPException mapping. The
      de-globalisation piece is `payments.validate_card()`, returning `(ok, reason, last4)`
      and never writing `session.LAST_CARD_DIGITS`; `create_booking` already takes the
      digits explicitly via `card_last4`.
- [x] Phase 4 — pilot endpoints + Pydantic models
      Shipped (commit 3): the six pilot endpoints (login/logout/me, availability,
      bookings, check-in) in api.py with schemas.py. The booking quote is computed
      server-side identically to the wizard, pay_kind is required (a booking must be
      backed by a ReservationPayments row), the card gate is payments.validate_card,
      and the audit actor comes from auth.audit_actor(principal). Endpoint tests live
      in tests/test_api.py (TestClient, services patched) ahead of the phase-6 test
      pass.
- [ ] Phase 5 — expand to remaining domains + reports
      Slice 1 shipped: the read-only report slice. Every reports.py report gained a
      `rows_*` data twin over one `_run_query`, with `export_*` as thin CSV adapters over
      the same SQL (one definition; verify_e2e reads the loyalty CSV back to prove the
      export path did not drift). `GET /api/reports/{name}` (staff-only) serves those
      rows, taking the CLI option names (customer/room, start/end, floor/date,
      booking_ref, limit).
      Slice 2 shipped: the read surfaces. `GET /api/rooms` (open) quotes from the same
      get_room_types() the wizard uses; `GET /api/reservations/board` (staff-only) wraps
      the existing non-interactive arrivals_departures_board() twin; `GET
      /api/guests/me/bookings` and `GET /api/guests/me/loyalty` (guest-only via a new
      require_guest dependency) serve the guest's own stays and loyalty, keyed on the
      cookie's verified CustomerID -- bookings.customer_stays() is the one new service
      twin, because the console resolves a stay by room/name and the web must not.
      Still to come in this phase: billing/folios, orders and admin read + write
      endpoints, each needing its own service twin.
- [ ] Phase 6 — API tests + docs/file-map updates

## Risks

- **CSRF** on cookie-auth writes — `SameSite` now; token if exposure widens.
- **New test dependency** (`httpx`, via `TestClient`). It is the only way to test the API
  from `unittest`; the alternative is to test services directly and skip HTTP tests.
- **`LAST_CARD_DIGITS`** must be de-globalised before any card-capturing endpoint ships.
- **Console internals change** as functions become adapters; prompt strings held stable to
  protect `verify_e2e.py`, proven by re-running the full suite.

## Constraints (repo rules this plan respects)

- **No DDL and no live-DB writes by the agent.** This design needs no migration; a new one
  would ship as `database.sql` + `migrations/0NN_*.sql` for the user to apply.
- Tests stay stdlib `unittest` (`pytest` is not installed).
- One commit per coherent change, signed, `git add` by name; push on `main` when green.
- Plaintext passwords are intentional; no hashing without an explicit request.
