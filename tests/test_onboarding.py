# type: ignore
"""Unit tests for the first-run onboarding path in main.py.

The wizard exists to close the one gap in docs/ONBOARDING.md that could not be closed in
the app: `Users` is seeded by nothing, and the only way to add one was behind a login. So
these cover the marker that decides whether setup runs at all, the first-account INSERT and
the guard that stops it becoming a back door, and the argument validation that keeps the
room seeder inside the bounds the rest of the app can actually reach.

Deliberately database-free: `get_connection` is stubbed with a fake cursor that records the
SQL it was handed, so a missing `?` placeholder or a wrong column name fails here rather
than against someone's database.

Run with:  python -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app


class FakeCursor:
    """Records every statement it is handed.

    `answers` is an ordered list of (substring, row) pairs; a statement takes the value of
    the FIRST pair whose substring it contains. Order is therefore significance: list the
    specific statements before the general ones, because `SELECT COUNT(*) FROM Users` is a
    substring of `SELECT COUNT(*) FROM Users WHERE Username = ?` and longest-match would
    otherwise answer the wrong probe.

    `raises` is a substring of the statement that should blow up, which is how the
    unapplied-migration path gets exercised.
    """

    def __init__(self, log, answers=(), raises=None):
        self.log = log
        self.answers = list(answers)
        self.raises = raises
        self.rowcount = 0
        self._next = None

    def execute(self, sql, params=()):
        self.log.append((sql, tuple(params or ())))
        if self.raises and self.raises in sql:
            raise RuntimeError("Invalid column/object name 'X'")
        for needle, value in self.answers:
            if needle in sql:
                self._next = value
                return
        self._next = None

    def fetchone(self):
        return self._next

    def fetchall(self):
        return self._next if isinstance(self._next, list) else ([] if self._next is None
                                                                 else [self._next])


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1


def run_with(log, answers=(), raises=None):
    """Patch get_connection with a recording fake cursor."""
    return mock.patch.object(app, "get_connection",
                             return_value=FakeConn(FakeCursor(log, answers, raises)))


def statements(log, needle):
    return [sql for sql, _ in log if needle in sql]


def params_for(log, needle):
    return [p for sql, p in log if needle in sql]


class OnboardingMarkerTests(unittest.TestCase):
    """onboarding_completed() gates the wizard, so its failures must resolve to 'run it'."""

    def test_absent_marker_means_not_onboarded(self):
        with mock.patch.object(app, "get_setting", return_value=None):
            self.assertFalse(app.onboarding_completed())

    def test_blank_marker_means_not_onboarded(self):
        with mock.patch.object(app, "get_setting", return_value="   "):
            self.assertFalse(app.onboarding_completed())

    def test_set_marker_means_onboarded(self):
        for raw in ("1", "true", "Y", "yes"):
            with mock.patch.object(app, "get_setting", return_value=raw):
                self.assertTrue(app.onboarding_completed(), raw)

    def test_explicit_zero_means_not_onboarded(self):
        with mock.patch.object(app, "get_setting", return_value="0"):
            self.assertFalse(app.onboarding_completed())

    def test_unreadable_settings_does_not_raise(self):
        # migration 023 absent, or HotelSettings locked: refusing to run setup here would
        # strand a fresh install nobody can log into.
        with mock.patch.object(app, "get_setting", side_effect=RuntimeError("no such table")):
            self.assertFalse(app.onboarding_completed())

    def test_marking_writes_the_one_setting(self):
        log = []
        with mock.patch.object(app, "set_setting", return_value=True) as setter:
            self.assertTrue(app.mark_onboarding_complete())
        setter.assert_called_once_with(app.ONBOARDING_SETTING, "1")


class CreateFirstUserTests(unittest.TestCase):
    """The one step the wizard exists for, and the guard that keeps it that way."""

    def _create(self, log, answers=(), raises=None, **kwargs):
        with run_with(log, answers, raises), mock.patch.object(app, "log_audit"):
            return app.create_first_user(**kwargs)

    def test_inserts_the_three_columns_add_user_does(self):
        log = []
        self.assertTrue(self._create(log, [("FROM Users", (0,))],
                                     username="admin", password="admin"))
        inserts = statements(log, "INSERT INTO Users")
        self.assertEqual(len(inserts), 1)
        # Same columns as add_user(), so the two paths cannot drift apart.
        self.assertIn("INSERT INTO Users (Username, Password, Role)", inserts[0])
        self.assertEqual(params_for(log, "INSERT INTO Users"), [("admin", "admin", "admin")])

    def test_the_insert_is_parameterized(self):
        # AGENTS.md section 4: every value goes through a placeholder, never string-formatted
        # into the statement.
        log = []
        self._create(log, [("FROM Users", (0,))], username="o'brien", password="p; DROP TABLE x")
        sql, params = [(s, p) for s, p in log if "INSERT INTO Users" in s][0]
        self.assertNotIn("o'brien", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertEqual(params, ("o'brien", "p; DROP TABLE x", "admin"))

    def test_refuses_when_an_account_already_exists(self):
        # The whole point of the guard. create_first_user() is reachable without a login
        # during first-run and must never mint a second admin on a live system.
        log = []
        self.assertFalse(self._create(log, [("FROM Users", (4,))],
                                      username="admin", password="admin"))
        self.assertEqual(statements(log, "INSERT"), [])

    def test_refuses_blank_credentials(self):
        for username, password in (("", "pw"), ("admin", ""), (None, None), ("   ", "pw")):
            log = []
            self.assertFalse(self._create(log, username=username, password=password),
                             (username, password))
            # Refused before it ever opened a connection.
            self.assertEqual(log, [], (username, password))

    def test_rejects_a_role_the_app_does_not_use(self):
        log = []
        self.assertFalse(self._create(log, username="bob", password="pw", role="wizard"))
        self.assertEqual(log, [])

    def test_a_master_named_row_is_accepted_under_a_loginable_role(self):
        # require_master_override() looks the account up BY NAME, so the wizard creates it
        # under that name -- but the role has to be one the Admin Panel can still render.
        log = []
        self.assertTrue(self._create(log, [("FROM Users", (0,))],
                                     username="master", password="pw", role="admin"))

    def test_the_master_row_is_created_with_a_role_that_can_sign_in(self):
        # Both onboarding paths make this account. Neither may use role 'master':
        # admin_panel() has no 'master' branch and add_user() offers only a/s/m, so such
        # a row is an account that exists but can never be signed in to.
        with mock.patch.object(app, "add_user_with_password") as add_user, \
             mock.patch.object(app, "user_exists", return_value=False), \
             mock.patch.object(app, "_prompt_new_password", return_value="pw"), \
             mock.patch.object(app, "setup_status", return_value=ChecklistGatingTests.READY), \
             mock.patch.object(app.ui, "pause"):
            with mock.patch("builtins.input",
                            ChecklistGatingTests()._scripted_input(["2", "7"])[0]):
                app.onboarding_checklist("admin")
        add_user.assert_called_once_with("master", "pw", "admin")

    def test_database_error_returns_false(self):
        log = []
        self.assertFalse(self._create(log, raises="FROM Users",
                                      username="admin", password="admin"))

    def test_no_connection_returns_false(self):
        with mock.patch.object(app, "get_connection", return_value=None):
            self.assertFalse(app.create_first_user("admin", "admin"))


class SeedItemsTests(unittest.TestCase):
    """`Items.ItemID` is a plain int PK, not identity, so id choice is the whole job."""

    def _seed(self, log, names=(), max_id=0, raises=None):
        answers = [("SELECT Name FROM Items", [(n,) for n in names]),
                   ("ISNULL(MAX(ItemID)", (max_id,))]
        with run_with(log, answers, raises), mock.patch.object(app, "log_audit"):
            return app.seed_default_items()

    def test_starts_past_the_highest_existing_id(self):
        # A re-run on a hotel that hand-added items must not die on a duplicate key.
        log = []
        self.assertEqual(self._seed(log, max_id=500), len(app.DEFAULT_SEED_ITEMS))
        self.assertEqual([p[0] for p in params_for(log, "INSERT INTO Items")],
                         list(range(501, 501 + len(app.DEFAULT_SEED_ITEMS))))

    def test_starts_at_one_on_an_empty_table(self):
        log = []
        self._seed(log, max_id=0)
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][0], 1)

    def test_null_max_id_is_treated_as_an_empty_table(self):
        log = []
        self.assertEqual(self._seed(log, max_id=None), len(app.DEFAULT_SEED_ITEMS))
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][0], 1)

    def test_is_idempotent_by_name_not_by_id(self):
        # An item the operator already added is left exactly as it is even though our id
        # would have been free.
        log = []
        added = self._seed(log, names=[n for _i, n, _p, _r in app.DEFAULT_SEED_ITEMS])
        self.assertEqual(added, 0)
        self.assertEqual(statements(log, "INSERT INTO Items"), [])

    def test_name_match_is_case_and_space_insensitive(self):
        log = []
        names = [" %s " % n.upper() for _i, n, _p, _r in app.DEFAULT_SEED_ITEMS]
        self.assertEqual(self._seed(log, names=names), 0)

    def test_partial_catalogue_only_adds_the_gaps(self):
        have = {app.DEFAULT_SEED_ITEMS[0][1], app.DEFAULT_SEED_ITEMS[3][1]}
        log = []
        self.assertEqual(self._seed(log, names=have, max_id=10),
                         len(app.DEFAULT_SEED_ITEMS) - 2)

    def test_added_ids_stay_contiguous_after_a_skip(self):
        have = {app.DEFAULT_SEED_ITEMS[0][1]}
        log = []
        self._seed(log, names=have, max_id=100)
        ids = [p[0] for p in params_for(log, "INSERT INTO Items")]
        self.assertEqual(ids, list(range(101, 101 + len(app.DEFAULT_SEED_ITEMS) - 1)))

    def test_never_sets_a_charge_group(self):
        # ChargeGroup lives on the Transactions row at billing time. Writing it here is how
        # loyalty points get counted twice.
        log = []
        self._seed(log)
        for sql in statements(log, "INSERT INTO Items"):
            self.assertNotIn("ChargeGroup", sql)

    def test_pricing_rules_are_the_ones_get_dynamic_price_understands(self):
        log = []
        self._seed(log)
        for sql, params in [(s, p) for s, p in log if "INSERT INTO Items" in s]:
            self.assertIn(params[3], (None, "Peak", "OffPeak"), params)

    def test_database_error_returns_zero(self):
        log = []
        self.assertEqual(self._seed(log, raises="FROM Items"), 0)


class StarterCatalogueContentTests(unittest.TestCase):
    """What DEFAULT_SEED_ITEMS is allowed to contain.

    Mostly a guard on one absence. The app posts the room charge itself at check-out as a
    Transactions row with ItemID = NULL and ChargeGroup = 'Room'. Seeding an orderable
    "Room Charge" item would be a double-charge footgun: record_transaction_for_room()
    defaults ChargeGroup to 'F&B', so adding it through Order Management posts an F&B line
    on top of the automatic room charge AND accrues loyalty points on it.
    """

    ROOM_CHARGE_WORDS = ("room charge", "room rate", "nightly rate", "accommodation")

    def test_no_room_charge_item_is_seeded(self):
        for _id, name, _price, _rule in app.DEFAULT_SEED_ITEMS:
            lowered = name.strip().lower()
            for word in self.ROOM_CHARGE_WORDS:
                self.assertNotIn(word, lowered,
                                 f"'{name}' would double-charge against the automatic "
                                 f"room charge (ItemID IS NULL, ChargeGroup 'Room')")

    def test_every_seeded_row_is_a_sellable_item(self):
        # This is what lets COUNT(*) > 0 stand as the catalogue readiness test: if a row
        # were not sellable, a hotel could satisfy the check while being unable to sell
        # anything, which is the failure the guard was refined to avoid.
        for _id, name, price, rule in app.DEFAULT_SEED_ITEMS:
            self.assertTrue(name.strip(), "seeded item with a blank name")
            self.assertGreaterEqual(price, 0.0, name)
            self.assertIn(rule, (None, "Peak", "OffPeak"), name)
            self.assertLessEqual(len(name), app.ITEM_NAME_MAX_LENGTH, name)

    def test_suggested_ids_are_distinct_and_contiguous_from_one(self):
        ids = [i for i, _n, _p, _r in app.DEFAULT_SEED_ITEMS]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))


class CustomItemEntryTests(unittest.TestCase):
    """Operator-typed items during setup.

    Three ways this can go wrong if it is not validated: a typed ItemID collides with an
    existing PK, a name longer than nvarchar(100) comes back from the driver as a truncation
    error, and a mistyped price raises ValueError. AGENTS.md section 3 is explicit that a
    prompt a guest can fumble must not produce a traceback, and the same applies here.
    """

    def _add(self, log, answers, names=(), max_id=0, raises=None, confirmations=(True,)):
        """Drive add_custom_item(). `answers` are the prompts in order."""
        db = [("SELECT Name FROM Items", [(n,) for n in names]),
              ("ISNULL(MAX(ItemID)", (max_id,))]
        queue = list(answers)
        with run_with(log, db, raises), \
             mock.patch.object(app, "log_audit"), \
             mock.patch("builtins.input", lambda prompt="": queue.pop(0) if queue else ""), \
             mock.patch.object(app.ui, "ask_confirmation",
                               side_effect=list(confirmations)):
            return app.add_custom_item()

    def test_item_id_continues_from_the_highest_existing(self):
        # Never asked for: ItemID is a plain int PK, so a typed id can collide and the
        # operator gets a constraint violation instead of an item.
        log = []
        self.assertTrue(self._add(log, ["Espresso", "4.50", ""], names=["Old Fashioned"],
                                  max_id=41))
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][0], 42)

    def test_first_item_on_an_empty_catalogue_gets_id_one(self):
        log = []
        self._add(log, ["Espresso", "4.50", ""])
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][0], 1)

    def test_a_name_that_is_only_whitespace_is_refused_then_accepted(self):
        log = []
        self.assertTrue(self._add(log, ["   ", "Espresso", "4.50", ""]))
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][1], "Espresso")

    def test_an_overlong_name_is_refused_rather_than_truncated_by_the_driver(self):
        # Name is nvarchar(100); longer raises "string or binary data would be truncated".
        log = []
        too_long = "x" * (app.ITEM_NAME_MAX_LENGTH + 1)
        self.assertTrue(self._add(log, [too_long, "Espresso", "4.50", ""]))
        self.assertEqual(len(params_for(log, "INSERT INTO Items")[0][1]),
                         len("Espresso"))

    def test_a_name_of_exactly_the_limit_is_accepted(self):
        log = []
        at_limit = "x" * app.ITEM_NAME_MAX_LENGTH
        self.assertTrue(self._add(log, [at_limit, "1.00", ""]))
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][1], at_limit)

    def test_an_unparseable_price_re_asks_and_writes_nothing_yet(self):
        log = []
        self.assertTrue(self._add(log, ["Espresso", "four fifty", "4.50", ""]))
        inserts = statements(log, "INSERT INTO Items")
        self.assertEqual(len(inserts), 1)
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][2], 4.50)

    def test_a_negative_price_is_refused(self):
        log = []
        self._add(log, ["Espresso", "-5", "4.50", ""])
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][2], 4.50)

    def test_a_free_item_is_allowed(self):
        log = []
        self._add(log, ["Local Guide Map", "0", ""])
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][2], 0.0)

    def test_price_is_rounded_to_the_column_scale(self):
        log = []
        self._add(log, ["Espresso", "4.567", ""])
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][2], 4.57)

    def test_blank_price_abandons_the_item_rather_than_writing_a_free_one(self):
        log = []
        self.assertFalse(self._add(log, ["Espresso", ""]))
        self.assertEqual(statements(log, "INSERT INTO Items"), [])

    def test_pricing_rule_accepts_the_same_spellings_as_add_item(self):
        for raw, expected in (("p", "Peak"), ("Peak", "Peak"), ("o", "OffPeak"),
                              ("off-peak", "OffPeak"), ("offpeak", "OffPeak"),
                              ("", None), ("nonsense", None)):
            with self.subTest(raw=raw):
                log = []
                self._add(log, ["Item %s" % raw, "1.00", raw])
                self.assertEqual(params_for(log, "INSERT INTO Items")[0][3], expected)

    def test_a_duplicate_name_warns_and_asks_before_writing(self):
        log = []
        self.assertTrue(self._add(log, ["Espresso", "4.50", ""], names=["espresso"],
                                  confirmations=(True,)))
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][1], "Espresso")

    def test_declining_the_duplicate_name_re_prompts_for_a_different_one(self):
        log = []
        self.assertTrue(self._add(log, ["Espresso", "Espresso Large", "5.00", ""],
                                  names=["espresso"], confirmations=(False,)))
        self.assertEqual(params_for(log, "INSERT INTO Items")[0][1], "Espresso Large")

    def test_no_connection_returns_false(self):
        with mock.patch.object(app, "get_connection", return_value=None):
            self.assertFalse(app.add_custom_item())

    def test_database_error_returns_false_instead_of_raising(self):
        log = []
        self.assertFalse(self._add(log, ["Espresso", "4.50", ""], raises="FROM Items"))


class OfferItemCatalogueTests(unittest.TestCase):
    """The menu that ties the starter set and custom entry together."""

    def _offer(self, log, answers, wizard=False, seeds=None, customs=(True,)):
        """Drive _offer_item_catalogue(). An answer that is an exception class is raised."""
        queue = list(answers)

        def fake_input(prompt=""):
            value = queue.pop(0) if queue else "3"
            if isinstance(value, type) and issubclass(value, BaseException):
                raise value()
            return value

        with mock.patch.object(app, "ui", create=True), \
             mock.patch.object(app, "seed_default_items",
                               return_value=0 if seeds is None else seeds) as seed, \
             mock.patch.object(app, "add_custom_item", side_effect=list(customs)) as custom, \
             mock.patch("builtins.input", fake_input):
            total = app._offer_item_catalogue(wizard=wizard)
        return total, seed, custom

    def test_starter_set_and_custom_entries_are_both_reachable(self):
        total, seed, custom = self._offer([], ["1", "2", "2", "3"], seeds=6,
                                          customs=(True, True))
        seed.assert_called_once()
        self.assertEqual(custom.call_count, 2)
        self.assertEqual(total, 8)

    def test_answering_3_immediately_adds_nothing_and_returns(self):
        total, seed, custom = self._offer([], ["3"])
        seed.assert_not_called()
        custom.assert_not_called()
        self.assertEqual(total, 0)

    def test_the_loop_survives_an_unrecognised_answer(self):
        total, seed, custom = self._offer([], ["9", "x", "3"])
        self.assertEqual(total, 0)

    def test_ctrl_c_ends_the_loop_and_keeps_what_was_added(self):
        # An operator who has added three items and then hits Ctrl+C must not lose them,
        # and must not get a traceback.
        total, seed, custom = self._offer([], ["2", "2", KeyboardInterrupt], seeds=0,
                                          customs=(True, True))
        self.assertEqual(total, 2)

    def test_eof_also_ends_the_loop_without_a_traceback(self):
        total, _seed, _custom = self._offer([], [EOFError])
        self.assertEqual(total, 0)

    def test_a_failed_custom_add_does_not_count(self):
        total, _seed, _custom = self._offer([], ["2", "3"], seeds=0, customs=(False,))
        self.assertEqual(total, 0)

    def test_wizard_and_checklist_differ_only_in_the_exit_wording(self):
        for wizard, expected in ((True, "Skip for now"), (False, "Cancel -- done adding items")):
            with self.subTest(wizard=wizard):
                fake_ui = mock.MagicMock()
                with mock.patch.object(app, "ui", fake_ui), \
                     mock.patch("builtins.input", lambda prompt="": "3"):
                    app._offer_item_catalogue(wizard=wizard)
                lines = fake_ui.show_menu.call_args[0][1]
                self.assertTrue(any(expected in ln for ln in lines), lines)


class RoomLayoutBoundsTests(unittest.TestCase):
    """Pure validation. No database, so the bounds are pinned here rather than in SSMS."""

    def test_tower_preset_is_always_valid(self):
        self.assertEqual(app.validate_room_layout(True), (True, 150, 999))

    def test_rectangular_bounds_are_inclusive(self):
        self.assertEqual(app.validate_room_layout(False, 1, 1), (False, 1, 1))
        self.assertEqual(app.validate_room_layout(False, 150, 999), (False, 150, 999))

    def test_rejects_out_of_range_and_junk(self):
        # 150 is view_rooms()'s own ceiling and 999 is the width of the 3-digit code.
        for floors, per in ((0, 10), (151, 10), (-1, 10),
                            (10, 0), (10, 1000), (10, -5),
                            (None, None), ("x", 5), (5, None), ("1.5", 5)):
            result = app.validate_room_layout(False, floors, per)
            self.assertFalse(result[0], (floors, per))
            self.assertIsNone(result[1], (floors, per))
            self.assertIsNone(result[2], (floors, per))

    def test_string_numbers_are_accepted(self):
        # The prompts read as text; the wizard should not have to pre-parse them.
        self.assertEqual(app.validate_room_layout(False, "20", "40"), (False, 20, 40))

    def test_a_rejected_layout_never_reports_itself_as_the_tower(self):
        # The flag is the first return value precisely so "invalid" cannot be read as
        # "fall back to seeding 138,180 rooms".
        self.assertFalse(app.validate_room_layout(False, 999, 999)[0])


class RoomSeedSqlTests(unittest.TestCase):
    """The generated statement is the one migration 008 runs, so its shape is load-bearing."""

    def _sql(self, tower=False, floors=None, rooms_per_floor=None, what="count"):
        flag, f, p = app.validate_room_layout(tower, floors, rooms_per_floor)
        log = []
        answers = [("SELECT COUNT(*)", (5,))] if what == "count" else []
        with run_with(log, answers):
            if what == "count":
                app.count_rooms_to_add(flag, floors=f, rooms_per_floor=p)
            else:
                app.seed_rooms(flag, floors=f, rooms_per_floor=p)
        needle = "SELECT COUNT(*) FROM Generated" if what == "count" else "INSERT INTO dbo.Rooms"
        return statements(log, needle)[0]

    def test_tower_reproduces_migration_008(self):
        sql = self._sql(True, what="insert")
        for fragment in ("f.Floor <= 100", "BETWEEN 101 AND 145 AND c.Code <= 850",
                         "BETWEEN 146 AND 150 AND c.Code BETWEEN 951 AND 956",
                         "'Presidential Suite'", "MAXRECURSION"):
            self.assertIn(fragment, sql, fragment)

    def test_every_statement_raises_the_recursion_ceiling(self):
        # The default is 100 and both CTEs recurse to 150 and 999, so without
        # MAXRECURSION 0 the statement dies with Msg 3609 on the operator's first click.
        for what in ("count", "insert"):
            self.assertIn("OPTION (MAXRECURSION 0)", self._sql(True, what=what), what)
            self.assertIn("OPTION (MAXRECURSION 0)",
                          self._sql(False, 20, 40, what=what), what)

    def test_tower_room_count_is_the_documented_figure(self):
        # docs/ONBOARDING.md and migration 008 both say 138,180.
        self.assertEqual(app._count_tower_rooms(), 138180)

    def test_placeholder_count_matches_parameter_count(self):
        # A mismatch is a pyodbc error at run time, on the one statement an operator waits
        # minutes for.
        for tower, floors, per in ((True, None, None), (False, 20, 40), (False, 1, 999)):
            log = []
            flag, f, p = app.validate_room_layout(tower, floors, rooms_per_floor=per)
            with run_with(log, [("SELECT COUNT(*)", (1,))]):
                app.count_rooms_to_add(flag, floors=f, rooms_per_floor=p)
            sql = statements(log, "SELECT COUNT(*) FROM Generated")[0]
            self.assertEqual(len(re.findall(r"\?", sql)), len(log[0][1]), (tower, floors, per))

    def test_rectangular_ladder_comes_from_the_python_tuple(self):
        sql = self._sql(False, 20, 40, what="insert")
        for _threshold, category in app.RECTANGULAR_ROOM_CATEGORIES:
            self.assertIn(f"'{category}'", sql)
        self.assertIn("ELSE 'Standard'", sql)

    def test_a_one_floor_hotel_gets_a_standard_room_not_a_penthouse(self):
        # Every threshold is a fraction, so floor 1 of 1 is 1.0 -- which is above all of
        # them. The ladder has to be evaluated against the floor, not the count.
        sql = self._sql(False, 1, 1, what="insert")
        self.assertIn("CAST(Floor AS decimal(10,4)) / ?", sql)

    def test_insert_never_updates_an_existing_room(self):
        # NOT EXISTS, not a merge: a room with a guest in it keeps its type and status, and
        # a partial seed can be topped up by running the wizard again.
        sql = self._sql(False, 2, 3, what="insert")
        self.assertIn("NOT EXISTS", sql)
        self.assertNotIn("UPDATE", sql.upper())

    def test_invalid_layout_writes_nothing(self):
        for kwargs in ({"floors": 151, "rooms_per_floor": 10},
                       {"floors": 10, "rooms_per_floor": 1000},
                       {"floors": None, "rooms_per_floor": None}):
            log = []
            with run_with(log):
                self.assertEqual(app.seed_rooms(**kwargs), 0)
            self.assertEqual(log, [], kwargs)

    def test_count_returns_none_when_the_query_fails(self):
        # None, not 0: "cannot tell" and "nothing to add" must not read the same, or the
        # wizard silently skips the step.
        with run_with([], raises="FROM Generated"):
            self.assertIsNone(app.count_rooms_to_add(floors=2, rooms_per_floor=3))

    def test_count_returns_none_for_an_invalid_layout(self):
        log = []
        with run_with(log):
            self.assertIsNone(app.count_rooms_to_add(floors=999, rooms_per_floor=1))
        self.assertEqual(log, [])

    def test_count_and_insert_enumerate_the_same_rooms(self):
        # If these drift, the number the operator is shown stops meaning what lands.
        count_log, insert_log = [], []
        with run_with(count_log, [("SELECT COUNT(*)", (7,))]):
            self.assertEqual(app.count_rooms_to_add(floors=9, rooms_per_floor=12), 7)
        with run_with(insert_log):
            app.seed_rooms(floors=9, rooms_per_floor=12)
        count_sql = statements(count_log, "SELECT COUNT(*) FROM Generated")[0]
        insert_sql = statements(insert_log, "INSERT INTO dbo.Rooms")[0]
        self.assertEqual(count_sql.split("SELECT COUNT")[0], insert_sql.split("INSERT INTO")[0])
        self.assertEqual(dict(count_log)[count_sql], dict(insert_log)[insert_sql])


class SetupStatusTests(unittest.TestCase):
    """A checklist that lies is worse than no checklist, so failure has to be visible."""

    def _status(self, log, answers=(), raises=None, settings=None, master_secret=""):
        # master_secret and loyalty are patched explicitly rather than inherited: config.ini
        # is untracked and per-machine, so a developer's own secret would otherwise decide
        # these assertions.
        with run_with(log, answers, raises), \
             mock.patch.object(app, "MASTER_SECRET", master_secret), \
             mock.patch.object(app, "LOYALTY_ENABLED", True), \
             mock.patch.object(app, "get_setting",
                               side_effect=lambda k, d=None: (settings or {}).get(k, d)):
            return dict((label, (done, detail)) for label, done, detail in app.setup_status())

    def _answers(self, users=0, master=0, items=0, rooms=0, rated=7, tiers=4):
        # Specific needles first: "FROM Users" is a substring of the master probe too.
        return [("FROM Users WHERE Username", (master,)),
                ("FROM RoomTypes", (rated,)),
                ("FROM LoyaltyTiers", (tiers,)),
                ("FROM Items", (items,)),
                ("FROM Rooms", (rooms,)),
                ("FROM Users", (users,))]

    def test_a_fresh_database_reports_the_runbook_gaps(self):
        checks = self._status([], self._answers(), settings={})
        self.assertFalse(checks["Staff accounts"][0])
        self.assertFalse(checks["Master override account"][0])
        self.assertFalse(checks["Item catalogue"][0])
        self.assertFalse(checks["Rooms"][0])
        self.assertFalse(checks["Business date"][0])
        # RoomTypes is seeded by database.sql, so a fresh install has this one already.
        self.assertTrue(checks["Nightly rates"][0])

    def test_an_empty_catalogue_explains_what_is_missing(self):
        # "empty" alone sends whoever reads it to the wrong menu. Worth naming that the
        # room charge is NOT one of these rows, because that is the mistaken assumption
        # that leads people to go looking for a "Room Charge" item that should not exist.
        checks = self._status([], self._answers(items=0), settings={})
        detail = checks["Item catalogue"][1]
        self.assertIn("nothing can be ordered", detail.lower())
        self.assertIn("one sellable item", detail.lower())

    def test_a_populated_database_reports_ready(self):
        answers = self._answers(users=3, master=1, items=12, rooms=138180)
        settings = {"business_date": "2026-10-02"}
        checks = self._status([], answers, settings=settings)
        for label in ("Staff accounts", "Master override account", "Item catalogue",
                      "Rooms", "Nightly rates", "Business date", "Loyalty tiers"):
            self.assertTrue(checks[label][0], label)

    def test_missing_master_row_is_a_failure_only_without_a_config_secret(self):
        answers = self._answers(users=1, master=0, items=3, rooms=3)
        checks = self._status([], answers, settings={}, master_secret="")
        self.assertFalse(checks["Master override account"][0])
        self.assertIn("master_secret", checks["Master override account"][1])
        checks = self._status([], answers, settings={}, master_secret="s3cret")
        self.assertTrue(checks["Master override account"][0])
        # A row and a configured secret both count; neither is required.
        checks = self._status([], self._answers(users=1, master=1, items=3, rooms=3),
                              settings={}, master_secret="")
        self.assertTrue(checks["Master override account"][0])

    def test_one_failing_probe_does_not_take_down_the_screen(self):
        # An unapplied migration should cost one red row, not the whole checklist.
        checks = self._status([], self._answers(users=2, master=1, items=5),
                              raises="FROM Rooms", settings={})
        self.assertFalse(checks["Rooms"][0])
        self.assertIn("migration", checks["Rooms"][1])
        # ...and the probes that did work are still reported.
        self.assertTrue(checks["Staff accounts"][0])
        self.assertTrue(checks["Item catalogue"][0])

    def test_unreachable_database_marks_everything_not_ready(self):
        with mock.patch.object(app, "get_connection", return_value=None), \
             mock.patch.object(app, "get_setting", return_value=None), \
             mock.patch.object(app, "MASTER_SECRET", ""):
            checks = dict((l, (d, x)) for l, d, x in app.setup_status())
        self.assertFalse(any(done for done, _ in checks.values()))
        self.assertTrue(any("migration" in detail for _done, detail in checks.values()))

    def test_business_date_is_checked_without_falling_back_to_the_wall_clock(self):
        # business_date() falls back to today, so a probe built on it could never fail.
        # The check asks get_setting() directly, which can.
        checks = self._status([], self._answers(users=1, master=1, items=1, rooms=1,
                                                rated=1), settings={})
        self.assertFalse(checks["Business date"][0])

    def test_loyalty_row_only_appears_when_loyalty_is_on(self):
        answers = self._answers(users=1, master=1, items=1, rooms=1, rated=1)
        with mock.patch.object(app, "LOYALTY_ENABLED", False), \
             mock.patch.object(app, "MASTER_SECRET", ""), \
             mock.patch.object(app, "get_setting", return_value="2026-10-02"):
            log = []
            with run_with(log, answers):
                labels = [l for l, _d, _x in app.setup_status()]
        self.assertNotIn("Loyalty tiers", labels)
        self.assertNotIn("FROM LoyaltyTiers", " ".join(sql for sql, _ in log))
        checks = self._status([], answers, settings={"business_date": "2026-10-02"})
        self.assertTrue(checks["Loyalty tiers"][0])


class ChecklistGatingTests(unittest.TestCase):
    """The checklist lives in the Admin Panel; minting an account must not bypass that."""

    READY = [("Staff accounts", True, "2 account(s)")]

    def _scripted_input(self, answers):
        """input() that walks a fixed script, then repeats the last answer.

        Returns (function, prompts) -- `prompts` records what was asked, so a test can
        assert the menu was never reached. Patching input with a plain function gives back
        the function itself, not a mock, so the call record has to be kept by hand.
        """
        queue = list(answers)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return queue.pop(0) if queue else answers[-1]

        return fake_input, prompts

    def test_manager_gets_the_read_only_view_and_no_action_menu(self):
        fake_input, prompts = self._scripted_input(["1"])
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", fake_input), \
             mock.patch.object(app, "add_user_with_password") as add_user, \
             mock.patch.object(app, "_offer_item_catalogue") as offer_items:
            app.onboarding_checklist("manager")
        add_user.assert_not_called()
        offer_items.assert_not_called()
        # It never even got as far as asking what to do.
        self.assertEqual(prompts, [])

    def test_admin_is_offered_the_actions(self):
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["7"])[0]), \
             mock.patch.object(app.ui, "show_menu") as show_menu:
            app.onboarding_checklist("admin")
        self.assertEqual(show_menu.call_args[0][0], "Setup actions")

    def test_the_item_action_opens_the_catalogue_menu_not_just_the_seeder(self):
        # An admin with a partially-filled catalogue needs to add their own item, which the
        # bare seed_default_items() call could not do.
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["3", "7"])[0]), \
             mock.patch.object(app, "_offer_item_catalogue", return_value=2) as offer_items, \
             mock.patch.object(app, "seed_default_items") as seed, \
             mock.patch.object(app.ui, "pause"):
            app.onboarding_checklist("admin")
        offer_items.assert_called_once_with()
        seed.assert_not_called()

    def test_the_catalogue_menu_is_not_opened_as_the_wizard(self):
        # wizard=True would label the exit "Skip for now", which reads wrong from a screen
        # an admin deliberately navigated to.
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["3", "7"])[0]), \
             mock.patch.object(app, "_offer_item_catalogue", return_value=0) as offer_items, \
             mock.patch.object(app.ui, "pause"):
            app.onboarding_checklist("admin")
        self.assertNotIn("wizard", offer_items.call_args.kwargs)

    def test_admin_adding_an_account_goes_through_the_logged_in_path(self):
        # Not create_first_user(): that one refuses once any account exists, which is
        # right for an unauthenticated wizard and wrong for a logged-in admin.
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["1", "bob", "s", "7"])[0]), \
             mock.patch.object(app, "user_exists", return_value=False), \
             mock.patch.object(app, "_prompt_new_password", return_value="pw"), \
             mock.patch.object(app, "add_user_with_password") as add_user, \
             mock.patch.object(app.ui, "pause"):
            app.onboarding_checklist("admin")
        add_user.assert_called_once_with("bob", "pw", "staff")

    def test_the_role_chosen_is_the_role_created(self):
        # A checklist that labelled the option "Add a staff account" but hardcoded
        # 'admin' was asking a question it then ignored.
        for letter, expected in (("a", "admin"), ("s", "staff"), ("m", "manager")):
            with self.subTest(role=expected):
                with mock.patch.object(app, "setup_status", return_value=self.READY), \
                     mock.patch("builtins.input",
                                self._scripted_input(["1", "bob", letter, "7"])[0]), \
                     mock.patch.object(app, "user_exists", return_value=False), \
                     mock.patch.object(app, "_prompt_new_password", return_value="pw"), \
                     mock.patch.object(app, "add_user_with_password") as add_user, \
                     mock.patch.object(app.ui, "pause"):
                    app.onboarding_checklist("admin")
                add_user.assert_called_once_with("bob", "pw", expected)

    def test_only_roles_the_admin_panel_can_render_are_offered(self):
        # admin_panel() branches on staff/manager/admin/valet/it, so offering a role with
        # no branch would create an account nobody can sign in to.
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch.object(app, "_prompt_role", return_value="manager") as prompt_role, \
             mock.patch("builtins.input", self._scripted_input(["1", "bob", "m", "7"])[0]), \
             mock.patch.object(app, "user_exists", return_value=False), \
             mock.patch.object(app, "_prompt_new_password", return_value="pw"), \
             mock.patch.object(app, "add_user_with_password"), \
             mock.patch.object(app.ui, "pause"):
            app.onboarding_checklist("admin")
        prompt_role.assert_called_once()

    def test_existing_username_is_not_re_inserted(self):
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["1", "bob", "a", "7"])[0]), \
             mock.patch.object(app, "user_exists", return_value=True), \
             mock.patch.object(app, "_prompt_new_password", return_value="pw"), \
             mock.patch.object(app, "add_user_with_password") as add_user, \
             mock.patch.object(app.ui, "pause"):
            app.onboarding_checklist("admin")
        add_user.assert_not_called()

    def test_role_check_is_case_insensitive(self):
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["7"])[0]), \
             mock.patch.object(app.ui, "show_menu") as show_menu:
            app.onboarding_checklist("ADMIN")
        show_menu.assert_called_once()

    def test_seven_returns_rather_than_looping_forever(self):
        # The menu must have a way out that does not depend on the database.
        with mock.patch.object(app, "setup_status", return_value=self.READY), \
             mock.patch("builtins.input", self._scripted_input(["7"])[0]), \
             mock.patch.object(app.ui, "show_menu") as show_menu:
            app.onboarding_checklist("admin")
        self.assertEqual(show_menu.call_count, 1)


class FirstRunGuardsTests(unittest.TestCase):
    """The wizard must never become a way to add an admin to a live system."""

    def test_marker_absence_drives_the_wizard_not_the_users_table(self):
        # main() asks onboarding_completed(), not "is Users empty". That is what stops the
        # wizard reappearing on a fully-configured database.
        with mock.patch.object(app, "onboarding_completed", return_value=True), \
             mock.patch.object(app, "run_first_run_onboarding") as wizard:
            with mock.patch.object(app, "handle_cli_args", return_value=True):
                app.main()
        wizard.assert_not_called()

    def test_a_cli_report_never_runs_the_wizard(self):
        # `python main.py --report revenue` has to stay non-interactive.
        with mock.patch.object(app, "onboarding_completed", return_value=False), \
             mock.patch.object(app, "handle_cli_args", return_value=True), \
             mock.patch.object(app, "run_first_run_onboarding") as wizard:
            app.main()
        wizard.assert_not_called()

    def test_an_incomplete_setup_runs_the_wizard_before_the_menu(self):
        with mock.patch.object(app, "onboarding_completed", return_value=False), \
             mock.patch.object(app, "handle_cli_args", return_value=False), \
             mock.patch.object(app, "run_first_run_onboarding") as wizard, \
             mock.patch.object(app, "ui", create=True), \
             mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                app.main()
        wizard.assert_called_once()


class WizardStepGuardsTests(unittest.TestCase):
    """An inherited database must not be offered a second round of seeding.

    Each optional step is gated on its own live probe. The marker alone is not enough: a
    database that was configured by hand or by a migration has no `onboarding_complete`
    row, so the wizard still runs -- and without these guards it would offer to seed a
    starter catalogue into a populated one.
    """

    def _answers(self, users=0, items=0, rooms=0, master=1):
        """Answers for setup_status()'s probes, specific needles first."""
        return [("FROM RoomTypes", (7,)),
                ("FROM LoyaltyTiers", (4,)),
                ("FROM Users WHERE Username", (master,)),
                ("FROM Items", (items,)),
                ("SELECT COUNT(*) FROM Rooms", (rooms,)),
                ("FROM Rooms", (rooms,)),
                ("FROM Users", (users,))]

    def _run(self, answers, answer="y"):
        """Run the whole wizard non-interactively. Tests patch the subject under test
        around this call; everything else the wizard touches is neutralised here.

        Settings written are recorded on `self.settings_written` rather than exposed as a
        mock: patching set_setting again inside this helper would shadow the outer mock, so
        the test's own mock would record nothing.

        The catalogue and room menus are stubbed. They are menus in their own right and have
        their own tests; left real here they would read this helper's scripted input ("y")
        and loop forever asking for a choice. `self.catalogue` and `self.rooms` are the mocks
        so a test can assert the wizard offered them at all.
        """
        self.settings_written = []
        self.catalogue = mock.MagicMock(return_value=0)
        self.rooms = mock.MagicMock()
        fake_ui = mock.MagicMock()
        fake_ui.ask_confirmation.return_value = True
        fake_ui.ask_number.side_effect = [20, 40]

        def record_setting(key, value, *a, **kw):
            self.settings_written.append((key, value))
            return True

        with mock.patch.object(app, "ui", fake_ui), \
             mock.patch.object(app, "set_setting", record_setting), \
             mock.patch.object(app, "log_audit"), \
             mock.patch.object(app, "_offer_item_catalogue", self.catalogue), \
             mock.patch.object(app, "_offer_room_layout", self.rooms), \
             mock.patch("builtins.input", lambda prompt="": answer), \
             mock.patch.object(app, "_prompt_new_password", return_value="pw"), \
             run_with([], answers):
            app.run_first_run_onboarding()
        return fake_ui

    def test_populated_catalogue_is_never_re_seeded(self):
        # 15 items already, none matching a DEFAULT_SEED_ITEMS name: a seed would add six
        # rows of stock nobody asked for.
        self._run(self._answers(users=6, items=15))
        self.catalogue.assert_not_called()

    def test_empty_catalogue_is_offered_as_a_menu_not_a_yes_no(self):
        # It used to be a single confirmation for the starter set. An operator whose hotel
        # sells none of those needs a way in, so this must be the loop that also takes
        # custom items, and it must be told it is running inside the wizard.
        self._run(self._answers(users=6, items=0))
        self.catalogue.assert_called_once_with(wizard=True)

    def test_populated_rooms_are_never_re_seeded(self):
        self._run(self._answers(users=6, items=15, rooms=138180))
        self.rooms.assert_not_called()

    def test_empty_rooms_are_offered(self):
        self._run(self._answers(users=6, items=15, rooms=0))
        self.rooms.assert_called_once_with(wizard=True)

    def test_existing_accounts_skip_login_creation_entirely(self):
        # create_first_user() refuses once any account exists. Retrying it in a loop would
        # spin forever, so the wizard must check first and say why it is skipping.
        with mock.patch.object(app, "create_first_user") as create:
            self._run(self._answers(users=6, items=15, rooms=138180))
        create.assert_not_called()

    def test_an_empty_database_does_get_the_first_login(self):
        # The first account must be an admin: an admin is the only role that reaches every
        # Admin Panel section, so a staff or valet bootstrap would lock the hotel out of
        # its own configuration.
        with mock.patch.object(app, "create_first_user", return_value=True) as create:
            self._run(self._answers(users=0, items=0, rooms=0))
        create.assert_called_once()
        self.assertEqual(create.call_args.args[2], "admin")

    def test_a_populated_database_still_gets_the_master_account_offered(self):
        # The master account is the fallback that makes the override work with a blank
        # master_secret, and it is exactly the step a hand-built database is most likely to
        # be missing -- so it is not gated on having zero rows.
        with mock.patch.object(app, "create_first_user", return_value=True) as create:
            self._run(self._answers(users=6, items=15, rooms=138180, master=0))
        create.assert_called_once()
        self.assertEqual(create.call_args.args[:2], ("master", "pw"))

    def test_both_created_accounts_get_a_role_that_can_log_in(self):
        # admin_panel() has branches for staff/manager/admin/valet/it and add_user() only
        # offers a/s/m. A 'master'-role row is an account nobody can ever sign in to, and
        # require_master_override() matches on the username alone, so it gains nothing.
        with mock.patch.object(app, "create_first_user", return_value=True) as create:
            self._run(self._answers(users=0, items=0, rooms=0, master=0))
        for call in create.call_args_list:
            self.assertEqual(call.args[2], "admin")

    def test_wizard_marks_itself_complete_on_the_way_out(self):
        # Otherwise it runs again on every start, forever.
        self._run(self._answers(users=6, items=15, rooms=138180))
        self.assertIn((app.ONBOARDING_SETTING, "1"), self.settings_written)

    def test_only_the_business_date_is_left_to_confirm(self):
        # With items, rooms and accounts all present, the business date is the one step
        # still worth asking about -- it is a clock, not a catalogue, and "today" is a
        # reasonable thing to offer. Anything else here means a guard is missing.
        fake_ui = self._run(self._answers(users=6, items=15, rooms=138180))
        asked = [c.args[0] for c in fake_ui.ask_confirmation.call_args_list]
        self.assertEqual(asked, ["Set it to today instead?"])
        self.assertTrue(fake_ui.box.called)


class TowerCountTests(unittest.TestCase):
    def test_arithmetic_matches_the_tiers(self):
        # 100 floors x 999 codes, 45 x 850, 5 x 6 -- the same three tiers as migration 008.
        self.assertEqual(app._count_tower_rooms(), 100 * 999 + 45 * 850 + 5 * 6)


if __name__ == "__main__":
    unittest.main()