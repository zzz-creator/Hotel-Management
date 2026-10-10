# PLAN — Split `main.py` into modules

Status: **DONE** (8 October 2026). Approved 8 October 2026.
Decisions confirmed by the user: Strategy B (clean modules, tests adapted), ~15-file split.

> **Update, 9 October 2026:** every module this plan created now lives in the `hotel/`
> package, with `main.py` (console) and `api.py` (web) left at the repository root as the
> only two entry points. Imports inside the package are relative (`from . import db`); the
> tier rule and the `__all__`-ownership rule below are unchanged, and the file names in the
> tables below are now under `hotel/`.

`main.py` was 8,998 lines with 257 functions. This plan moved them into domain modules
without changing behaviour. **See the "What actually happened" section at the end for the
handful of places the build diverged from the design below.**

---

## What actually happened (deviations from this plan)

- **16 domain modules, not 15.** A `notifications.py` was added: `_concierge_inbox` /
  `_feedback_inbox` (tier-4 `concierge`) are reached from `admin.py` (tier 5) and the
  notification helpers are reached from several tiers, so notifications sits on tier 1 and
  breaks what would otherwise be an `admin ↔ customer` cycle.
- **`patch_main` resolves ownership from each module's `__all__`**, not from
  `getattr(app, name).__module__` as sketched in §3.1. After the split `app.<name>` is the
  star-imported copy, whose `__module__` is the owner — that works for functions, but the
  `__all__` scan also covers constants and data, and it is what the splitter already had to
  hand-maintain. `get_connection` → `db`, `config`/`config_path` → `core`, `ui`/`db`
  (wholesale module patches) → every consuming module, and the session names → `session` are
  special-cased.
- **Shadowed module names are aliased.** A local variable can share a module's name (e.g.
  `_admin_menu` keeps a local list `reservations`; `customer_panel` keeps a string
  `session`). `tools/split_main.py` detects this and imports the module as
  `_mod_<name>` for the whole file, so the qualified call cannot read the local.
- **`_RESERVATIONS_CAPTURED_RATE_SUPPORT` stayed in class-3 `reservations.py`**, not
  `session.py` as §3.1 implied; `_RESERVATION_PAYMENTS_PARTIAL_SUPPORT` and
  `_INVOICES_PREPAID_SUPPORT` live in `session.py` with the rest of the mutable state.
- **The splitter is `tools/split_main.py`**, AST-driven, and regenerates every module from
  the old monolith. The split is done, so `main.py` is now the facade and the tool reads its
  input from `--source` (default `main.py`); re-running means `git show
  pre-main-split:main.py > main.py` first, then restoring the facade. It refuses to run
  against a file with fewer than 200 top-level defs, so a forgotten restore fails loudly.

---


## 0. Revert point (before any code moves)

```powershell
git tag -a pre-main-split -m "main.py monolith before the module split"
git push origin pre-main-split   # optional, keeps the point off-machine
```

A tag is an immovable pointer at the current HEAD (`1e26351`), so any later commit can be
undone with `git switch -d pre-main-split` without rewriting history.

## 1. Target architecture

Tier rule: a module may import only from **lower** tiers. This is what keeps the split
from becoming a circular-import mess.

| Tier | File | Contents (moved from `main.py`) |
|---|---|---|
| 0 | `db.py`, `ui.py`, `reports.py`, `clearance_ui.py` | unchanged |
| 1 | **`session.py`** | mutable globals: `CURRENT_USER`, `CURRENT_CUSTOMER`, `LAST_CARD_DIGITS` |
| 1 | **`core.py`** | `config` / `CONNECTION_STRING` / `db.init`, all `DEFAULT_*` and `CHARGE_GROUP_*` constants, `get_setting` / `set_setting` / `_setting_float` / `_setting_int` and the ~15 settings getters, `log_audit`, `require_master_override`, `business_date`, `reservation_window_active`, `_existing_tables`, `_scalar_count`, `_is_integrity_code`, `_is_duplicate_key_error`, `CustomFormatter` |
| 2 | **`rooms.py`** | room status/type/rate helpers (`get_room_status`, `set_room_status`, `get_room_type`, multipliers, `get_nightly_rate`, `update_room_type_rate`, `ensure_room_types_seeded`, captured-rate helpers), `view_rooms`, `rooms_dashboard`, `update_room_status`, `rooms_admin_menu`, `view_room_type_rates`, `upsert_room_if_missing`, room seeding/layout (`seed_rooms`, `validate_room_layout`, `_rooms_generation_sql`, `count_rooms_to_add`, `_count_tower_rooms`), `ROOM_STATUSES` / `UNBOOKABLE_ROOM_STATUSES` / `MAX_ROOM_FLOORS` / `MAX_ROOMS_PER_FLOOR` |
| 2 | **`items.py`** | `display_items`, `get_quantity`, `get_another_item`, `add_item`, `delete_item`, `update_item`, `view_items`, `get_dynamic_price`, `get_item_choice`, `manage_pricing_rules`, `_parse_pricing_rule`, `seed_default_items`, `_insert_item`, `add_custom_item`, `_ask_item_name`, `_ask_item_price`, `comp_item_to_room` |
| 2 | **`loyalty.py`** | `ensure_loyalty_tables`, customer/room point helpers, `redeem_points_for_invoice`, `LoyaltyRedemptionError`, tiers (`_tiers_from_db`, `get_tier_*`, `recompute_*`), `award_stay_points`, `award_billed_order_points`, `points_per_dollar_order_vs_room`, and the loyalty admin screens (`loyalty_admin_menu`, `admin_view_loyalty_*`, `admin_adjust_loyalty_points`, `admin_manage_loyalty_tiers`, `admin_recompute_all_tiers`, `_pick_loyalty_customer`), `view_my_loyalty_status` stays in `customer.py` (guest-facing) |
| 2 | **`payments.py`** | `luhn_check`, `process_credit_card`, `validate_expiration_date` |
| 2 | **`keycards.py`** | `_new_card_number`, `issue_key_card`, `revoke_active_key_cards`, `move_key_cards`, `get_active_key_card`, `get_key_cards_for_room`, `_log_door_event`, `try_open_door`, `revoke_key_card`, `door_access_menu`, `view_my_key_card` |
| 3 | **`reservations.py`** | `normalize_room_number`, `validate_room`, `VALIDATE_ROOM_MAX_ATTEMPTS`, `add_reservation`, `_archive_row`, `archive_reservation`, `delete_reservation`, `delete_all_reservations`, `edit_reservation`, `view_reservations`, `search_reservations`, `_reservation_check_in`, `_book_reservation_in_conn`, `link_reservation_customer`, `check_in`, `search_availability`, `arrivals_departures_board`, `show_arrivals_departures_board`, `show_availability_search`, `_show_housekeeping_rooms`, `stay_nights`, `stays_overlap`, `_reservations_have_captured_rate` cache |
| 3 | **`booking_ledger.py`** | `booking_quote`, `booking_payment_options`, `settle_with_prepayment`, `refund_decision`, `booking_refund_policy`, `get_booking_refund_cutoff_days`, `_write_booking_charge`, `new_booking_ref`, `booking_reference_exists`, `BookingRefTaken`, `_is_*` helpers already in core, `_original_charge_card`, `record_booking_payment`, `get_booking_paid_total`, `allocate_booking_credit`, `get_outstanding_booking_credit`, `apply_booking_credit`, `_apply_credit_legacy`, `_set_partial_credit_unsupported`, `BOOKING_REF_PREFIX` / `BOOKING_DEPOSIT_NIGHTS` / `BOOKING_REFUND_CUTOFF_DAYS_DEFAULT` / `PAYMENT_KIND_*` |
| 3 | **`billing.py`** | `record_transaction_for_room`, `post_room_charge`, `compute_and_apply_discounts`, `apply_discount`, `bill_room_transactions`, `billing_creator`, `list_invoices_for_room`, `print_invoice`, `invoices_menu`, `void_invoice`, `refund_invoice`, `settlement_outstanding`, `announce_settlement_outstanding`, `check_out`, `_invoices_have_prepaid_column`, `_build_invoice_insert`, `_set_partial…` companion bits |
| 3 | **`orders.py`** | `open_order`, `add_order_items`, `order_item`, `track_order_status`, `get_orders_for_room`, `get_order_items`, `manage_orders_menu`, `next_order_status`, `advance_order`, `get_amenities`, `view_amenities`, `manage_amenities_menu`, `get_promotions`, `view_promotions`, `manage_promotions_menu`, `provide_feedback` |
| 4 | **`bookings.py`** | the public desk: `book_room`, `_find_booking`, `_own_booking`, `view_my_booking`, `cancel_booking`, `booking_panel`, `show_availability_search` (if not kept in reservations — pick one home and note it here) |
| 4 | **`customer.py`** | `register_customer`, `customer_login`, `_customer_profile`, `customer_session_label`, `upsert_customer_profile`, `customer_panel`, `load_customer_history`, `show_customer_history`, `my_history`, `search_customer_profiles`, `view_my_loyalty_status`, `print_my_invoice`, guest-account admin (`_pick_customer_account`, `create_guest_account`, `_update_account_field`, `edit_guest_account`, `reset_guest_password`, `delete_guest_account`, `link_stay_to_guest_account`) |
| 4 | **`concierge.py`** | `contact_concierge`, `view_my_concierge_requests`, `guest_requests_menu`, `_concierge_inbox`, `_feedback_inbox` |
| 5 | **`admin.py`** | `admin_login`, `_prompt_role`, `_admin_menu`, `_run_admin_submenu`, `admin_panel`, user CRUD (`add_user`, `delete_user`, `edit_user`, `view_users`, `reset_user_password`, `_prompt_new_password`, `user_exists`, `add_user_with_password`, `clear_lockout`), notifications & staff alerts (`send_notification_to_customer`, `view_notifications_for_room`, `send_alert_to_staff`, `view_staff_alerts`), discount codes (`view_discount_codes`, `manage_discount_codes`), pricing/settings editors (`_edit_general_settings`, `reset_settings_to_defaults`, `_offer_hotel_settings`), reports (`_run_report`, `export_reports_menu`), IT & valet panels (`it_*`, `valet_vehicle_management`, `_valet_list_parked`, `_pick_from_list`) |
| 5 | **`onboarding.py`** | `onboarding_completed`, `mark_onboarding_complete`, `create_first_user`, `setup_status`, `run_first_run_onboarding`, `onboarding_checklist`, `_prompt_new_password` (shared with admin — one home, import the other), `_offer_item_catalogue`, `_offer_room_layout`, `_offer_hotel_settings` (shared — same rule), `_count_tower_rooms` (shared with rooms — same rule), `_ensure_database_config` |
| — | **`main.py`** | facade re-exporting every name, `handle_cli_args()`, `main()` menu loop |

Shared-helper rule: a helper used by two modules gets **one** home in the lower tier; the
other imports it module-qualified. Duplicate definitions are forbidden.

## 2. Governing rules for the move

1. **Cross-module calls are module-qualified**: `billing.post_room_charge(...)`, never
   `from billing import post_room_charge`. Then every name has exactly one binding — its
   owner's — and a test patch hits every caller. (`from core import get_setting` is also
   forbidden for the same reason: two bindings, one patched.)
2. **Connections only via `db.get_connection()`** — never `from db import get_connection`.
   One shared module object means one patch target for all 64 `get_connection` test sites.
3. **Session state lives in `session.py`**, accessed as `session.CURRENT_USER` etc.
   `global CURRENT_USER` becomes `session.CURRENT_USER = ...` (attribute assignment; the
   `global` statement cannot qualify). Write sites: `admin_login`, `customer_login`,
   `booking_panel`, `customer_panel`, plus the `log_audit` read and the ~10 other reads.
4. **Tier imports only.** An upward need gets a function-local `import` *and* a note back to
   the user — never a silent cycle. Known constraint already handled by the table:
   `billing` needs booking-credit functions, so those live in tier-3 `booking_ledger.py`,
   not tier-4 `bookings.py`.
5. **`main.py` is the facade**: every moved name is re-exported so `import main as app`
   keeps working for reads and assertions (`app.BookingRefTaken`, `app.<fn>(...)`), and
   `get_connection = db.get_connection` stays — `verify_e2e.py` asserts the identity
   (`main.get_connection is db.get_connection`).
6. Each new file keeps `# type: ignore` on line 1 and the existing style: f-string logging,
   `with get_connection() as conn` blocks with the `conn is None` guard, no classes beyond
   the three that already exist (`CustomFormatter`, `LoyaltyRedemptionError`,
   `BookingRefTaken`).

## 3. Test adaptation (Strategy B)

The suite does `mock.patch.object(app, "<name>")` **301 times**; only ~60 distinct names.
Top targets: `get_connection` ×64, `get_setting` ×20, `log_audit` ×18,
`get_loyalty_enabled` ×17, `CURRENT_CUSTOMER` ×17, `setup_status` ×11.

1. **Helper** (in `tests/`, importable when `tests/` is on `sys.path`):

   ```python
   def patch_main(name, **kwargs):
       """Patch `name` where it is resolved, not where main re-exports it."""
       target = SPECIAL.get(name) or sys.modules[getattr(app, name).__module__]
       return mock.patch.object(target, name, **kwargs)
   ```

   `SPECIAL` table for non-functions: `get_connection` → `db`, `config` / `config_path` →
   `core`, `CURRENT_USER` / `CURRENT_CUSTOMER` / `LAST_CARD_DIGITS` → `session`,
   `VALIDATE_ROOM_MAX_ATTEMPTS` → `reservations`.
   (Verify each entry against where the name actually resolves at move time; the table is
   derived from `getattr(app, name).__module__` for functions, hand-listed only for data.)
2. **One regex rewrite** across `tests/*.py`:
   `mock.patch.object(app, "` → `patch_main("`, then add the import to each file.
3. **~10 hand fixes**: wholesale module patches `mock.patch.object(app, "ui", ...)` (4),
   `app.db` (2), `app.config`/`app.config_path` (5) must target the *consuming* module's
   binding (e.g. `mock.patch.object(onboarding, "ui", ...)`) or be converted to patching
   functions on the shared module object.
4. **`verify_e2e.py`**: `app._RESERVATIONS_CAPTURED_RATE_SUPPORT = None` must set the
   attribute on the owning module (`reservations`), not the facade;
   `app.CONNECTION_STRING = ...` / `app.database = ...` need the same check (harmless if
   only `dbmod.init()` matters, but confirm). All `app.<fn>(...)` calls there work through
   the facade unchanged.
5. **Sanity guard**: after the rewrite, `grep` for any remaining
   `mock.patch.object(app, "` — there must be none.

Failures under this strategy are loud (missing attribute / mock never called), which is
the point. A test that silently stops patching the right namespace is the risk this
helper exists to prevent.

## 4. Execution order (one commit per batch, green before committing)

1. Tag (`pre-main-split`).
2. Scaffold `session.py` + `core.py`; rewire the 6 state-write sites; adapt the constant
   patch sites; tests green → commit.
3. Test helper + regex rewrite (step 3.1–3.3 above); tests green → commit.
   *(Doing the helper before the moves means every batch after this stays green.)*
4. Tier 2: `rooms.py`, `items.py`, `loyalty.py`, `payments.py`, `keycards.py` → commit.
5. Tier 3: `reservations.py`, `booking_ledger.py`, `billing.py`, `orders.py` → commit.
6. Tier 4: `bookings.py`, `customer.py`, `concierge.py` → commit.
7. Tier 5: `admin.py`, `onboarding.py`; slim `main.py` to facade + entry point → commit.
8. Docs (step 5) → commit.

Per batch: `python -m py_compile <all moved files>` + `python -m unittest discover -s tests`.
`git add` by name, never `-A`.

## 5. Docs (checker-enforced — `tests/check_docs_sync.py` will fail otherwise)

- `CODE_FILES` in `tests/check_docs_sync.py` gains every new module (check 2 scans these
  files for function definitions).
- AGENTS.md §2 file map: add every new file (check 4), add this PLAN file to the design-docs
  row.
- AGENTS.md §5: extend the `py_compile` verification line to all modules.
- AGENTS.md + `docs/DEVIATIONS.md`: rewrite stale `main.py:NNNN` references as function
  names (AGENTS §5/§7 has ~6, DEVIATIONS has 5) so they never rot again.
- `docs/BOOKING.md` / `docs/SCHEMA.md`: only if a named function's home changed in a way
  the checker notices — run it and see.

## 6. Final verification (all must exit 0)

```powershell
python -m py_compile main.py db.py reports.py ui.py <every new module>
python -m unittest discover -s tests
python tests/check_schema_sync.py
python tests/check_migration_sql.py
python tests/check_docs_sync.py
python tests/check_applied_migrations.py
python tests/verify_e2e.py            # disposable DB; creates and drops its own
```

`verify_e2e.py` is the important one: it exercises the booking/loyalty/billing flows
through `app.<fn>` and holds the two regression probes for the `main.get_connection`
alias. Non-zero exit there is a real finding, not a harness quirk — do not loosen the
assertion that caught it.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Circular imports between modules | Tier rule (§2.4); verify with `python -c "import main"` after each batch |
| Session globals duplicated per namespace (the `python main.py` vs `import main` double-copy trap) | State lives in `session.py`, which every module imports normally — there is only ever one copy |
| A test patch silently hitting the facade instead of the resolving module | Helper + the "no `from x import fn`" rule + the final grep guard (§3.5) |
| `check_docs_sync` red at the end | Docs batch (step 5) is its own commit, run before committing |
| Behaviour drift | No logic is rewritten — bodies move verbatim; only import/qualification lines change |
