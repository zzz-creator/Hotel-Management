# type: ignore
"""Unit tests for the split Admin Panel: the data-driven menu and its dispatch.

The contract pinned here is capability parity: restructuring the flat per-role
menus into category submenus must not change what a role can reach. The expected
sets below are EXACTLY what the flat menus reached before the split (admin had
34 entries, manager 22, staff 13, excluding Exit) -- a role that gains or loses
a function is a permission change, not a menu reorganisation.

Run with:  python -m unittest discover -s tests
"""

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
from patch_main import patch_main

_STAFF = {
    "add_reservation", "view_reservations", "edit_reservation", "delete_reservation",
    "search_reservations", "show_availability_search", "show_arrivals_departures_board",
    "guest_requests_menu", "manage_orders_menu", "view_staff_alerts",
    "rooms_admin_menu", "comp_item_to_room", "open_clearance_window",
}

_MANAGER = _STAFF - {"comp_item_to_room"} | {
    "send_notification_to_customer", "add_item", "delete_item", "update_item",
    "view_items", "view_users", "view_discount_codes", "invoices_menu",
    "search_customer_profiles", "door_access_menu",
}

_GUEST_ACCOUNT_FUNCS = {
    "create_guest_account", "edit_guest_account", "reset_guest_password",
    "delete_guest_account", "link_stay_to_guest_account", "search_customer_profiles",
}

_ADMIN = _STAFF - {"comp_item_to_room"} | _GUEST_ACCOUNT_FUNCS | {
    "send_notification_to_customer", "send_alert_to_staff", "add_item",
    "delete_item", "update_item", "view_items", "manage_amenities_menu",
    "manage_promotions_menu", "add_user", "delete_user", "edit_user",
    "view_users", "reset_user_password", "manage_discount_codes",
    "manage_pricing_rules", "loyalty_admin_menu", "export_reports_menu",
    "invoices_menu", "search_customer_profiles", "door_access_menu",
    "delete_all_reservations", "onboarding_checklist",
}

_ADMIN_ONLY = {
    "manage_amenities_menu", "manage_promotions_menu", "send_alert_to_staff",
    "delete_all_reservations", "loyalty_admin_menu", "export_reports_menu",
    "manage_pricing_rules", "add_user", "delete_user", "edit_user",
    "reset_user_password",
}


def _entry_names(menu):
    names = set()
    for _category, entries in menu:
        if entries is None:
            continue
        for _label, func, _kwargs in entries:
            names.add(func.__name__)
    return names


def _categories(menu):
    return {category: entries for category, entries in menu}


class AdminMenuParityTests(unittest.TestCase):
    """A role must reach exactly the functions the flat menus gave it."""

    def test_staff_parity(self):
        self.assertEqual(_entry_names(app._admin_menu("staff")), _STAFF)

    def test_manager_parity(self):
        self.assertEqual(_entry_names(app._admin_menu("manager")), _MANAGER)

    def test_admin_parity(self):
        self.assertEqual(_entry_names(app._admin_menu("admin")), _ADMIN)

    def test_each_role_has_exactly_one_exit_entry_and_it_is_last(self):
        for role in ("staff", "manager", "admin"):
            menu = app._admin_menu(role)
            self.assertEqual(menu[-1], ("Exit Admin Panel", None))
            self.assertEqual([c for c, e in menu if e is None], ["Exit Admin Panel"])

    def test_every_entry_is_callable_and_binds_its_declared_kwargs(self):
        for role in ("staff", "manager", "admin"):
            for category, entries in app._admin_menu(role):
                if entries is None:
                    continue
                for label, func, kwargs in entries:
                    self.assertTrue(callable(func), f"{role}/{category}: {label}")
                    try:
                        inspect.signature(func).bind(**kwargs)
                    except TypeError as exc:
                        self.fail(f"{role}/{category}: {label} ({func.__name__}) {exc}")

    def test_no_staff_or_manager_role_ships_the_admin_only_entries(self):
        for role in ("staff", "manager"):
            leaked = _entry_names(app._admin_menu(role)) & _ADMIN_ONLY
            self.assertEqual(leaked, set())

    def test_view_only_rooms_and_door_match_the_old_branches(self):
        staff = _categories(app._admin_menu("staff"))
        manager = _categories(app._admin_menu("manager"))
        admin = _categories(app._admin_menu("admin"))
        self.assertEqual(dict(staff["Rooms & Housekeeping"][0][2]), {"view_only": True})
        self.assertEqual(dict(manager["Rooms & Housekeeping"][0][2]), {"view_only": True})
        self.assertEqual(dict(admin["Rooms & Housekeeping"][0][2]), {})
        manager_door = {f.__name__: k for _l, f, k in manager["Security & Access"]}
        admin_door = {f.__name__: k for _l, f, k in admin["Security & Access"]}
        self.assertEqual(dict(manager_door["door_access_menu"]), {"view_only": True})
        self.assertEqual(dict(admin_door["door_access_menu"]), {})

    def test_admin_accounts_category_carries_users_and_guest_accounts(self):
        admin = _categories(app._admin_menu("admin"))
        names = {f.__name__ for _l, f, _k in admin["Accounts"]}
        self.assertLessEqual(_GUEST_ACCOUNT_FUNCS | {
            "add_user", "delete_user", "edit_user", "view_users", "reset_user_password",
        }, names)

    def test_manager_cannot_reach_the_guest_account_mutations(self):
        self.assertEqual(_entry_names(app._admin_menu("manager")) & _GUEST_ACCOUNT_FUNCS,
                         {"search_customer_profiles"})


class AdminSubmenuDispatchTests(unittest.TestCase):
    """The submenu renderer and dispatcher read the same tuples."""

    def test_a_choice_runs_that_entry_with_its_declared_kwargs(self):
        seen = []

        def fake(**kwargs):
            seen.append(kwargs)

        entries = [("One", fake, {}), ("Two", fake, {"view_only": True})]
        with mock.patch.object(app.ui, "show_menu"), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch("builtins.input", side_effect=["2", "3"]):
            app._run_admin_submenu("Test", entries, "Signed in as desk01 (staff)")
        # Entry "2" ran with its kwargs; "3" was Back, so nothing else ran.
        self.assertEqual(seen, [{"view_only": True}])

    def test_invalid_choices_are_rejected_not_crashed(self):
        def fake():
            raise AssertionError("an entry must not run")

        entries = [("One", fake, {})]
        with mock.patch.object(app.ui, "show_menu"), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch("builtins.input", side_effect=["x", "0", "2"]):
            app._run_admin_submenu("Test", entries, "s")
        # Returns via "Back"; the rejected picks only drew messages.

    def test_the_back_entry_returns_without_running_anything(self):
        def fake():
            raise AssertionError("an entry must not run")

        entries = [("One", fake, {})]
        with mock.patch.object(app.ui, "show_menu"), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch("builtins.input", return_value="2"):
            app._run_admin_submenu("Test", entries, "s")


class AdminPanelTests(unittest.TestCase):
    """admin_panel() drives the data menu; role stubs are unchanged."""

    def test_staff_sees_the_category_menu_and_exits(self):
        with patch_main("admin_login", return_value=(True, "staff", False)), \
             patch_main("CURRENT_USER", "desk01"), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu, \
             mock.patch("builtins.input", return_value="5"):
            app.admin_panel()
        # Staff has 5 categories; 5 = Exit Admin Panel.
        self.assertEqual(menu.call_count, 1)
        self.assertEqual(menu.call_args.kwargs.get("subtitle"), "Signed in as desk01 (staff)")

    def test_choosing_a_category_opens_its_submenu(self):
        with patch_main("admin_login", return_value=(True, "staff", False)), \
             patch_main("CURRENT_USER", "desk01"), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu"), \
             patch_main("_run_admin_submenu") as submenu, \
             mock.patch("builtins.input", side_effect=["1", "5"]):
            app.admin_panel()
        submenu.assert_called_once()
        self.assertEqual(submenu.call_args[0][0], "Reservations Management")
        self.assertEqual(submenu.call_args[0][2], "Signed in as desk01 (staff)")

    def test_valet_runs_its_panel_without_a_menu(self):
        with patch_main("admin_login", return_value=(True, "valet", False)), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu, \
             patch_main("valet_vehicle_management") as flow:
            app.admin_panel()
        flow.assert_called_once()
        menu.assert_not_called()

    def test_an_unknown_role_gets_a_message_not_a_menu(self):
        with patch_main("admin_login", return_value=(True, "janitor", False)), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu:
            app.admin_panel()
        menu.assert_not_called()

    def test_reauth_does_not_fall_through_into_a_second_session(self):
        # The outer frame used to recurse and then CONTINUE into its menu loop once
        # the inner session ended -- a second live session on the unlocked role with
        # no password entered for it. The tell is the number of menu renders: the
        # inner session draws exactly one, and a fallen-through outer frame draws a
        # second. A third admin_login would also be a fall-through, but "11" exits
        # the admin menu before one is needed, so the menu count is the real pin.
        with patch_main("admin_login",
                               side_effect=[(False, "admin", True), (True, "admin", False)]) as login, \
             patch_main("CURRENT_USER", "desk01"), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu, \
             mock.patch("builtins.input", return_value="11"):
            app.admin_panel()
        # Admin has 11 categories; 11 = Exit Admin Panel.
        self.assertEqual(login.call_count, 2)
        self.assertEqual(menu.call_count, 1)