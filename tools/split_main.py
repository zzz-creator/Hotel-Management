# type: ignore
"""One-shot, re-runnable splitter for main.py -> domain modules.

This is a *tool*, not application code: it reads the monolith, moves each top-level
statement to its target module (MAPPING below), rewrites every reference to a name that
now lives in another module as a module-qualified attribute (billing.post_room_charge,
session.CURRENT_USER, ...), generates the imports each module needs, and prints the
resulting import graph so cycles are caught before anything is written.

Why it exists rather than a hand move: main.py is ~9,000 lines with ~250 functions, and
the repository's comments are load-bearing. Slicing by AST line ranges keeps every byte
of body and comment, only inserting `owner.` prefixes at exact Name-token offsets.

Rules it enforces (agreed in PLAN-split-main-py.md):
  * cross-module references are module-qualified, so each name has exactly one binding
    and patching the owner module affects every caller;
  * mutable process state lives in session.py and is written as session.X = ... ;
  * no module imports main;
  * get_connection is always db.get_connection (main keeps the alias only for the
    identity probe in tests/verify_e2e.py).

Usage:
    python tools/split_main.py            # dry run: report graph/cycles, write nothing
    python tools/split_main.py --apply    # write the new module files
    python tools/split_main.py --source <path> [--apply]

The split is DONE, so `main.py` is now the facade and no longer the monolith this tool
consumes. Re-running therefore needs the monolith restored from the tag first:

    git show pre-main-split:main.py > main.py
    python tools/split_main.py --apply
    git checkout -- main.py              # put the facade back

The tool refuses to run against a file that does not look like the monolith (fewer than 200
top-level defs), so a forgotten restore fails loudly instead of rewriting every module from
the facade.
"""
import ast
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'main.py')


def resolve_source():
    """`--source PATH` (relative to the repo root) or main.py."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == '--source' and i + 1 < len(argv):
            return os.path.join(ROOT, argv[i + 1])
        if arg.startswith('--source='):
            return os.path.join(ROOT, arg.split('=', 1)[1])
    return SOURCE


CORE = 'core'
SESSION = 'session'

# ---------------------------------------------------------------------------
# 1. Where each top-level statement goes.
# ---------------------------------------------------------------------------
MAPPING = {}
MAPPING.update({n: CORE for n in (
    # config bootstrap
    'config', 'config_path', 'server', 'database', 'username', 'password',
    'CONNECTION_STRING', '_ensure_database_config',
    # constants
    'DEFAULT_TIERS', 'DEFAULT_ROOM_TYPE_MULTIPLIERS', 'DEFAULT_ROOM_TYPE_RATES',
    'DEFAULT_HOTEL_SETTINGS', 'CHARGE_GROUP_ROOM', 'CHARGE_GROUP_FNB', 'STAFF_ROLES',
    'BOOKING_REFUND_CUTOFF_DAYS_DEFAULT',
    # duplicate-key detection
    '_DUPLICATE_KEY_NUMBERS', '_DUPLICATE_KEY_GENERIC_STATE',
    '_DUPLICATE_KEY_GENERIC_STATE_INT', '_is_integrity_code', '_is_duplicate_key_error',
    # settings
    'get_setting', 'set_setting', '_setting_float', '_setting_int', 'get_peak_factor',
    'get_offpeak_factor', 'get_tax_rate', 'get_hotel_name', 'get_loyalty_enabled',
    'get_lockout_threshold', 'get_lockout_duration', 'get_customer_login_max_attempts',
    'get_loyalty_accrual_points_per_unit',
    'get_loyalty_redemption_points_per_currency_unit', 'get_loyalty_points_per_night',
    # audit / overrides / dates
    'log_audit', 'require_master_override', 'business_date', 'reservation_window_active',
    # pure helpers shared across modules
    'stay_nights', 'stays_overlap', 'generate_code',
    # shared guest gate and small lookups every domain needs (kept in core so the
    # domain modules do not import each other sideways -- see the cycle report)
    'normalize_room_number', 'VALIDATE_ROOM_MAX_ATTEMPTS', 'validate_room',
    '_reservation_check_in', 'get_amenities', 'get_booking_refund_cutoff_days',
    # settings editors reached from the item/pricing menu as well as the admin panel
    '_edit_general_settings', 'reset_settings_to_defaults',
    # role prompt shared by admin.py and onboarding.py
    '_prompt_role',
    # small DB helpers
    '_existing_tables', '_scalar_count',
    # user-account primitives shared by admin.py and onboarding.py
    'user_exists', 'add_user_with_password', '_prompt_new_password',
    # guest-profile write, called from reservations/booking desk as well as customer.py
    'upsert_customer_profile',
)})
MAPPING.update({n: SESSION for n in (
    'CURRENT_USER', 'CURRENT_CUSTOMER', 'LAST_CARD_DIGITS',
    '_RESERVATIONS_CAPTURED_RATE_SUPPORT', '_RESERVATION_PAYMENTS_PARTIAL_SUPPORT',
    '_INVOICES_PREPAID_SUPPORT',
)})
MAPPING.update({n: 'rooms' for n in (
    'ROOM_STATUSES', 'UNBOOKABLE_ROOM_STATUSES', 'MAX_ROOM_FLOORS', 'MAX_ROOMS_PER_FLOOR',
    'RECTANGULAR_ROOM_CATEGORIES', '_ROOMS_DESCRIPTION_SQL',
    'get_room_status', 'upsert_room_if_missing', 'set_room_status', 'get_room_type',
    '_room_type_in_conn', '_room_status_in_conn', '_room_type_setting_key',
    'get_room_type_multiplier', 'get_all_room_type_multipliers', 'get_room_types',
    'get_nightly_rate', '_nightly_rate_in_conn', '_rate_or_none', 'stay_nightly_rate',
    '_reservations_have_captured_rate', 'get_captured_nightly_rate', 'update_room_type_rate',
    'ensure_room_types_seeded', 'rooms_dashboard', 'view_rooms', 'update_room_status',
    'rooms_admin_menu', 'view_room_type_rates', '_rooms_generation_sql',
    'validate_room_layout', 'count_rooms_to_add', 'seed_rooms',
)})
MAPPING.update({n: 'items' for n in (
    'ITEM_NAME_MAX_LENGTH', 'DEFAULT_SEED_ITEMS',
    'display_items', 'get_quantity', 'get_another_item', 'add_item', 'delete_item',
    'update_item', 'view_items', 'get_dynamic_price', 'get_item_choice',
    'manage_pricing_rules', '_parse_pricing_rule', 'seed_default_items', '_insert_item',
    'add_custom_item', '_ask_item_name', '_ask_item_price', 'comp_item_to_room',
)})
MAPPING.update({n: 'loyalty' for n in (
    'ensure_loyalty_tables', 'customer_id_for_stay', 'ensure_customer_loyalty_account',
    'get_points_by_customer', 'add_points_to_customer', 'redeem_points_by_customer',
    'LoyaltyRedemptionError', 'redeem_points_for_invoice', 'get_lifetime_points_by_customer',
    'create_loyalty_account_if_missing', 'get_points_by_room', 'add_points_by_room',
    'redeem_points_by_room', 'get_lifetime_points_by_room', '_tiers_from_db',
    'get_tier_for_points', 'get_tier_details_by_customer', 'get_tier_details_by_room',
    'recompute_tier_by_customer', 'recompute_tier', 'recompute_all_tiers',
    'award_stay_points', 'award_billed_order_points', 'points_per_dollar_order_vs_room',
    'loyalty_admin_menu', '_pick_loyalty_customer', 'admin_view_loyalty_accounts',
    'admin_view_loyalty_transactions', 'admin_adjust_loyalty_points',
    'admin_manage_loyalty_tiers', 'admin_recompute_all_tiers',
)})
MAPPING.update({n: 'payments' for n in (
    'luhn_check', 'process_credit_card', 'validate_expiration_date',
)})
MAPPING.update({n: 'keycards' for n in (
    'KEY_CARD_PREFIX', '_new_card_number', 'issue_key_card', 'revoke_active_key_cards',
    'move_key_cards', 'get_active_key_card', 'get_key_cards_for_room', '_log_door_event',
    'try_open_door', 'revoke_key_card', 'door_access_menu', 'view_my_key_card',
)})
MAPPING.update({n: 'reservations' for n in (
    'RESERVATION_RESET_SCOPES', 'RESERVATION_RESET_CONFIRM',
    'add_reservation', '_archive_row',
    'archive_reservation', 'delete_reservation', 'delete_all_reservations',
    'edit_reservation', 'view_reservations', 'search_reservations',
    '_book_reservation_in_conn', 'link_reservation_customer', 'check_in',
    'search_availability', 'arrivals_departures_board', 'show_arrivals_departures_board',
    '_show_housekeeping_rooms', 'show_availability_search',
)})
MAPPING.update({n: 'booking_ledger' for n in (
    'BOOKING_REF_PREFIX', 'BOOKING_DEPOSIT_NIGHTS',
    'PAYMENT_KIND_DEPOSIT', 'PAYMENT_KIND_PREPAYMENT', 'PAYMENT_KIND_REFUND',
    'PAYMENT_KIND_FORFEIT', 'BOOKING_REF_WRITE_ATTEMPTS',
    'booking_quote', 'booking_payment_options', 'settle_with_prepayment', 'refund_decision',
    'booking_refund_policy', '_write_booking_charge',
    'new_booking_ref', 'booking_reference_exists', 'BookingRefTaken', '_original_charge_card',
    'record_booking_payment', 'get_booking_paid_total', 'allocate_booking_credit',
    'get_outstanding_booking_credit', 'apply_booking_credit', '_apply_credit_legacy',
    '_set_partial_credit_unsupported',
)})
MAPPING.update({n: 'billing' for n in (
    'record_transaction_for_room', 'post_room_charge', 'compute_and_apply_discounts',
    'apply_discount', 'bill_room_transactions', 'billing_creator', 'list_invoices_for_room',
    'print_invoice', 'invoices_menu', 'void_invoice', 'refund_invoice',
    'settlement_outstanding', 'announce_settlement_outstanding', 'check_out',
    '_invoices_have_prepaid_column', '_build_invoice_insert',
)})
MAPPING.update({n: 'orders' for n in (
    'ORDER_STATUSES', 'ORDER_STATUS_HELP', 'open_order', 'add_order_items', 'order_item',
    'view_amenities', 'provide_feedback', 'get_promotions',
    'view_promotions', 'track_order_status', 'get_orders_for_room', 'get_order_items',
    'manage_orders_menu', 'next_order_status', 'advance_order', 'manage_amenities_menu',
    'manage_promotions_menu',
)})
MAPPING.update({n: 'bookings' for n in (
    'book_room', '_find_booking', '_own_booking', 'view_my_booking', 'cancel_booking',
    'booking_panel',
)})
MAPPING.update({n: 'customer' for n in (
    'register_customer', 'customer_login', '_customer_profile', 'customer_session_label',
    'view_my_loyalty_status', 'print_my_invoice', 'load_customer_history',
    'show_customer_history', 'my_history', 'search_customer_profiles',
    '_pick_customer_account', 'create_guest_account', '_update_account_field',
    'edit_guest_account', 'reset_guest_password', 'delete_guest_account',
    'link_stay_to_guest_account', 'customer_panel',
)})
MAPPING.update({n: 'concierge' for n in (
    'contact_concierge', 'view_my_concierge_requests', 'guest_requests_menu',
    '_concierge_inbox', '_feedback_inbox',
)})
MAPPING.update({n: 'admin' for n in (
    'admin_login', '_admin_menu', '_run_admin_submenu', 'admin_panel',
    'add_user', 'delete_user', 'edit_user', 'view_users', 'reset_user_password',
    'clear_lockout',
    'view_discount_codes',
    'manage_discount_codes',
    '_run_report', 'export_reports_menu', 'IT_SYSTEMS', 'IT_SOFTWARE',
    'IT_TROUBLESHOOTING', '_pick_from_list', 'it_network_configuration',
    'it_user_accounts', 'it_system_diagnostics', 'it_software_installation',
    'it_support_panel', 'valet_vehicle_management', '_valet_list_parked',
)})
MAPPING.update({n: 'notifications' for n in (
    'send_notification_to_customer', 'view_notifications_for_room',
    'send_alert_to_staff', 'view_staff_alerts',
)})
MAPPING.update({n: 'onboarding' for n in (
    'ONBOARDING_SETTING', 'onboarding_completed', 'mark_onboarding_complete',
    'create_first_user', 'setup_status', '_offer_hotel_settings',
    'run_first_run_onboarding', 'onboarding_checklist', '_offer_item_catalogue',
    '_offer_room_layout', '_count_tower_rooms',
)})

# Statements with no bindable name (Expr / For) are assigned by their line number.
BY_LINE = {
    23: CORE,       # config.read(config_path)
    113: CORE,      # db.init(CONNECTION_STRING)
    1368: 'items',  # commented-out get_item_choice block (a string Expr)
}

# References always resolved through another module instead of being defined here.
REPLACE = {'get_connection': 'db.get_connection'}

MODULES = ['core', 'session', 'rooms', 'items', 'loyalty', 'payments', 'keycards',
           'reservations', 'booking_ledger', 'billing', 'orders', 'notifications',
           'bookings', 'customer', 'concierge', 'admin', 'onboarding']
PROJECT_MODULES = set(MODULES)

IMPORT_MAP = {
    'logging': 'import logging', 'os': 'import os', 'sys': 'import sys',
    'time': 'import time', 'random': 'import random', 'string': 'import string',
    'getpass': 'import getpass', 'configparser': 'import configparser',
    'argparse': 'import argparse', 're': 'import re', 'tqdm': 'import tqdm',
    'Decimal': 'from decimal import Decimal', 'date': 'from datetime import date',
    'datetime': 'from datetime import datetime', 'timedelta': 'from datetime import timedelta',
    'db': 'import db', 'ui': 'import ui', 'reports': 'import reports',
    'clearance_ui': 'import clearance_ui', 'session': 'import session',
}
STDLIB_NAMES = ['logging', 'os', 'sys', 'time', 'random', 'string', 'getpass',
                'configparser', 'argparse', 're', 'tqdm', 'Decimal', 'date', 'datetime',
                'timedelta']
INFRA_NAMES = ['db', 'ui', 'reports', 'clearance_ui', 'session']
DUNDER = {'__name__', '__file__', '__doc__', '__builtins__', '__loader__', '__spec__',
          '__package__'}


def builtin_names():
    import builtins
    return set(dir(builtins)) | DUNDER


BUILTINS = builtin_names()


# ---------------------------------------------------------------------------
# 2. Scope analysis: which Name references are globals (not locals/params).
# ---------------------------------------------------------------------------
def bound_names(fn):
    """Names bound in this function's own scope (excluding nested function scopes)."""
    result = set()
    args = fn.args
    for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        result.add(a.arg)
    if args.vararg:
        result.add(args.vararg.arg)
    if args.kwarg:
        result.add(args.kwarg.arg)
    for comp in ast.walk(fn):
        if isinstance(comp, ast.comprehension):
            for tgt in [comp.target]:
                for sub in ast.walk(tgt):
                    if isinstance(sub, ast.Name):
                        result.add(sub.id)
    declared_global = set()

    class Visitor(ast.NodeVisitor):
        def visit_Global(self, node):
            declared_global.update(node.names)

        def visit_Nonlocal(self, node):
            declared_global.update(node.names)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                result.add(node.id)

        def visit_FunctionDef(self, node):
            result.add(node.name)          # do not descend: nested scope

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            result.add(node.name)

        def visit_Lambda(self, node):
            pass

    visitor = Visitor()
    for stmt in fn.body:
        visitor.visit(stmt)
    return result - declared_global


class Collector:
    """Collects module-qualification edits and `global` lines to drop."""

    def __init__(self, module, force_alias=()):
        self.module = module
        self.replacements = []      # (abs_lineno, col, end_col, original, replacement)
        self.drop_lines = set()     # absolute line numbers
        self.referenced = collections.Counter()
        self.edge_names = collections.defaultdict(set)
        # Module names that a local variable shadows somewhere in this module. References
        # to a shadowed module go through `import X as _mod_X`; see _ref().
        self.force_alias = set(force_alias)
        self.aliased = set()

    def _ref(self, owner, local_names):
        """How to name cross-module owner `owner` in this scope.

        A local variable can share a module's name (e.g. `_admin_menu` keeps a local list
        called `reservations`). Writing `reservations.add_reservation` there would read the
        local, so the module is imported under `_mod_reservations` and used everywhere in
        this module (force_alias), keeping the reference unambiguous.
        """
        if owner in self.force_alias or owner in local_names:
            self.aliased.add(owner)
            return '_mod_' + owner
        return owner

    def qualify(self, node, local_names, base):
        name = node.id
        if name in local_names:
            return
        if name in REPLACE:
            owner, _, attr = REPLACE[name].partition('.')
            self.referenced[owner] += 1
            self.edge_names[owner].add(name)
            replacement = self._ref(owner, local_names) + '.' + attr
            self.replacements.append((base + node.lineno - 1, node.col_offset,
                                      node.end_col_offset, name, replacement))
            return
        if name in IMPORT_MAP:
            self.referenced[name] += 1
            return
        if name not in MAPPING:
            return
        owner = MAPPING[name]
        if owner == self.module:
            return
        self.referenced[owner] += 1
        self.edge_names[owner].add(name)
        replacement = '%s.%s' % (self._ref(owner, local_names), name)
        self.replacements.append((base + node.lineno - 1, node.col_offset,
                                  node.end_col_offset, name, replacement))


def _arg_names(args):
    names = {a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def visit(node, local_names, collector, base):
    """Qualify Name references, recursing into nested scopes with their own locals.

    A nested function must be walked with `enclosing locals | its own bindings` as the
    non-qualifiable set: a closure variable is not a module global, but a `global X` two
    levels down still is. Skipping nested bodies (as an earlier version did) left
    `LAST_CARD_DIGITS` and friends bare inside helpers like `_write`, so they raised
    NameError only when the helper actually ran.
    """
    if isinstance(node, ast.Name):
        collector.qualify(node, local_names, base)
        return
    if isinstance(node, ast.Lambda):
        visit(node.body, set(local_names) | _arg_names(node.args), collector, base)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # Decorators, defaults and annotations evaluate in the *enclosing* scope.
        for extra in list(getattr(node, 'decorator_list', [])) + \
                list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            visit(extra, local_names, collector, base)
        inner = set(local_names) | bound_names(node)
        for stmt in node.body:
            if isinstance(stmt, ast.Global) and all(
                    n in MAPPING and MAPPING[n] != collector.module
                    for n in stmt.names):
                collector.drop_lines.add(base + stmt.lineno - 1)
            visit(stmt, inner, collector, base)
        return
    if isinstance(node, ast.ClassDef):
        for extra in getattr(node, 'decorator_list', []):
            visit(extra, local_names, collector, base)
        for stmt in node.body:
            visit(stmt, set(local_names), collector, base)
        return
    for child in ast.iter_child_nodes(node):
        visit(child, local_names, collector, base)


def _collect(module, parts, force_alias=()):
    collector = Collector(module, force_alias)
    for start, end, text in parts:
        tree = ast.parse(text)
        for stmt in tree.body:
            visit(stmt, set(), collector, start)
    return collector


def collect_module(module, parts):
    """Collect edits, then re-collect once if any module name is shadowed by a local.

    The first pass discovers shadowing as it goes (Collector._ref). If it found any, the
    second pass applies the alias for the whole module so every reference to that owner is
    consistent -- some call sites shadow it and some do not, and mixing `reservations.x`
    with `_mod_reservations.x` in one module is exactly the ambiguity this avoids.
    """
    first = _collect(module, parts)
    if not first.aliased:
        return first
    return _collect(module, parts, force_alias=first.aliased)


# ---------------------------------------------------------------------------
# 3. Slice, qualify, emit.
# ---------------------------------------------------------------------------
def build():
    source_path = resolve_source()
    with open(source_path, encoding='utf-8') as handle:
        source = handle.read()
    if source.count('\ndef ') + source.count('\nasync def ') < 200:
        raise SystemExit(
            "%s does not look like the main.py monolith (found %d top-level defs).\n"
            "main.py is now the facade; restore the monolith from the tag first:\n"
            "    git show pre-main-split:main.py > main.py"
            % (source_path, source.count('\ndef ')))
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)

    chunks = []
    prev_end = 0
    for stmt in tree.body:
        chunks.append((prev_end + 1, stmt.end_lineno, stmt))
        prev_end = stmt.end_lineno
    chunks.append((prev_end + 1, len(lines), None))

    dest = {}
    for start, end, stmt in chunks:
        if stmt is None:
            continue
        module = None
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module = MAPPING.get(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in MAPPING:
                    module = MAPPING[tgt.id]
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            module = MAPPING.get(stmt.target.id)
        if module is None and stmt.lineno in BY_LINE:
            module = BY_LINE[stmt.lineno]
        dest[start] = module

    module_chunks = collections.defaultdict(list)
    for start, end, stmt in chunks:
        if dest.get(start):
            module_chunks[dest[start]].append((start, end, ''.join(lines[start - 1:end])))

    collectors = {m: collect_module(m, parts) for m, parts in module_chunks.items()}

    # Fail loudly if any edit's token does not match what we expect.
    bad = []
    for module, coll in collectors.items():
        for ln, col, endcol, original, replacement in coll.replacements:
            token = lines[ln - 1][col:endcol]
            if token != original:
                bad.append((module, ln, col, endcol, original, token))
    if bad:
        for row in bad[:20]:
            print('OFFSET MISMATCH', row)
        raise SystemExit('%d bad offsets; refusing to write' % len(bad))

    return lines, chunks, dest, module_chunks, collectors


def module_names(parts):
    """The top-level names a module binds, in source order (its defs/classes/assigns)."""
    names = []
    for _, _, text in parts:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(stmt.name)
            elif isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        names.append(tgt.id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names.append(stmt.target.id)
    return names


def render_module(module, edited_parts, collectors):
    if module == 'session':
        body = ''.join(text for _, _, text in edited_parts[module])
        header = ('# type: ignore\n'
                  '"""Process-wide mutable session state.\n\n'
                  'Split out of main.py so a module can hold state without importing the\n'
                  'entry point. Tests patch these through the owning module, and main.py\n'
                  'forwards attribute reads here (module __getattr__), so `app.CURRENT_USER`\n'
                  'still reads the live value.\n"""\n')
        return header + body, [], []

    coll = collectors[module]
    needed = set(coll.referenced)
    header_imports = []
    seen_imports = set()

    def import_line(name):
        # A shadowed module is imported under an alias the whole module uses (see _ref).
        if name in coll.aliased:
            return 'import %s as _mod_%s' % (name, name)
        return IMPORT_MAP.get(name, 'import %s' % name)

    for name in STDLIB_NAMES + INFRA_NAMES:
        if name in needed:
            line = import_line(name)
            if line not in seen_imports:
                header_imports.append(line)
                seen_imports.add(line)
    for name in sorted(n for n in needed if n in PROJECT_MODULES and n != module):
        line = import_line(name)
        if line not in seen_imports:
            header_imports.append(line)
            seen_imports.add(line)
    unknown = sorted(n for n in needed
                     if n not in IMPORT_MAP and n not in PROJECT_MODULES
                     and n not in BUILTINS)
    body = ''.join(text for _, _, text in edited_parts[module])
    names = module_names(edited_parts[module])
    header = ('# type: ignore\n'
              '"""%s: split out of main.py. Cross-module calls are module-qualified so a '
              'test patching the owner module affects every caller (see '
              'PLAN-split-main-py.md)."""\n' % module)
    header += '\n'.join(header_imports) + '\n\n'
    # __all__ lists every name the module binds, private helpers included, so main.py's
    # `from <module> import *` facade exposes the whole surface (a bare star import would
    # skip the underscore-prefixed helpers the tests patch).
    header += '__all__ = [\n'
    for name in names:
        header += '    %r,\n' % name
    header += ']\n\n\n'
    return header + body, unknown, header_imports


def main():
    lines, chunks, dest, module_chunks, collectors = build()

    # Apply edits + drops to a working copy.
    work = list(lines)
    for module, coll in collectors.items():
        for ln in coll.drop_lines:
            work[ln - 1] = ''
    by_line = collections.defaultdict(list)
    for module, coll in collectors.items():
        for ln, col, endcol, original, replacement in coll.replacements:
            by_line[ln].append((col, endcol, original, replacement))
    for ln, items in by_line.items():
        text = work[ln - 1]
        for col, endcol, original, replacement in sorted(items, key=lambda t: -t[0]):
            text = text[:col] + replacement + text[endcol:]
        work[ln - 1] = text

    # Re-cut chunks from the edited copy.
    edited_chunks = []
    prev_end = 0
    for start, end, stmt in chunks:
        edited_chunks.append((start, end, stmt, ''.join(work[start - 1:end])))

    rebuilt = collections.defaultdict(list)
    main_out = []
    for start, end, stmt, text in edited_chunks:
        module = dest.get(start)
        if module:
            rebuilt[module].append((start, end, text))
        else:
            main_out.append((start, end, text))

    reports = {}
    rendered = {}
    for module in MODULES:
        if module not in module_chunks:
            continue
        text, unknown, header_imports = render_module(module, rebuilt, collectors)
        rendered[module] = text
        reports[module] = (unknown, header_imports)

    # Report the graph.
    print('== statement counts ==')
    for module in MODULES:
        print('%-16s %3d' % (module, len(module_chunks.get(module, []))))
    print('== import graph ==')
    graph = {}
    for module in MODULES:
        if module not in reports:
            continue
        deps = [d.split()[1] for d in reports[module][1]
                if d.startswith('import ') and d.split()[1] in PROJECT_MODULES
                and d.split()[1] != module]
        graph[module] = deps
        print('%-16s -> %s' % (module, ', '.join(deps) or '(none)'))
    print('== unknown references (need a home or an import) ==')
    for module, (unknown, _) in reports.items():
        if unknown:
            print('%-16s %s' % (module, unknown))
    cycles = find_cycles(graph)
    print('== cycles ==')
    print(cycles or '(none)')
    print('== edges (who calls whom) ==')
    for module in MODULES:
        if module not in collectors:
            continue
        edges = collectors[module].edge_names
        for other in sorted(edges):
            if other in PROJECT_MODULES and other != module:
                print('%-16s -> %-16s %s' % (module, other, sorted(edges[other])))
    print('== duplicate top-level names across modules ==')
    seen = collections.defaultdict(set)
    for name, module in MAPPING.items():
        seen[name].add(module)
    print({n: sorted(m) for n, m in seen.items() if len(m) > 1} or '(none)')

    if '--apply' not in sys.argv:
        print('\n(dry run; pass --apply to write)')
        return

    for module in MODULES:
        if module in rendered:
            path = os.path.join(ROOT, module + '.py')
            with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(rendered[module])
            print('wrote %s (%d lines)' % (module + '.py', rendered[module].count('\n')))
    with open(os.path.join(ROOT, 'tools', '_main_leftover.txt'), 'w',
              encoding='utf-8', newline='\n') as handle:
        handle.write(''.join(text for _, _, text in main_out))
    print('wrote tools/_main_leftover.txt')


def find_cycles(graph):
    cycles = []
    colour = {}

    def visit(node, stack):
        colour[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if colour.get(dep) == 1:
                cycles.append(stack[stack.index(dep):] + [dep])
            elif colour.get(dep) is None:
                visit(dep, stack)
        stack.pop()
        colour[node] = 2

    for node in graph:
        if node not in colour:
            visit(node, [])
    return cycles


if __name__ == '__main__':
    main()
