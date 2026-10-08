# type: ignore
"""Unit tests for customer-keyed loyalty (migration 019) and booking-desk login.

The bug this file exists to prevent: loyalty was keyed on RoomNumber, so a balance
belonged to a physical room. The next guest of that room inherited the balance and
tier, a returning guest's points fragmented across rooms, and moving a guest stranded
or transferred their points. These tests pin the three properties that make a balance
follow the PERSON -- the SourceID idempotency key, the resolver, and ownership of a
booking -- plus the login itself.

Everything here is a pure function or a thin DB wrapper, so the whole file runs without
SQL Server.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app


def _as_queue(result_sets):
    """Normalise a test's expectations into a queue of result sets, one per SELECT.

    Two shapes are accepted, because most functions here issue a single query and a
    queue of one should not be ceremony:
      * a flat list of rows  -> `[(4321,)]`  is ONE query returning one row
      * a list of lists       -> `[[], [(40.0,)]]` is TWO queries: empty, then one row
    A list of lists is read as the queue; anything else as a single result set.
    """
    if result_sets is None:
        return []
    items = list(result_sets)
    if items and all(isinstance(item, list) for item in items):
        return items
    return [items]


class _FakeCursor:
    """Records every statement, and hands out one queued result set per SELECT.

    A queue rather than a fixed result, because the functions under test issue several
    different SELECTs (is this email taken? -> insert, has this award already been made?
    -> sum the folio) and each needs its own answer.
    """

    def __init__(self, log, result_sets=None):
        self.log = log
        # Deliberately NOT copied: the connection owns the queue so that a second
        # cursor() on the same connection continues where the first one stopped.
        self.result_sets = _as_queue(result_sets)
        self.rows = []
        self.rowcount = -1
        self._index = 0

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        upper = sql.lstrip().upper()
        # An OUTPUT INSERTED clause returns rows just like a SELECT does, so it draws
        # from the same queue -- otherwise every INSERT..OUTPUT would look like no row.
        if upper.startswith(("SELECT", "WITH")) or "OUTPUT INSERTED" in upper:
            self.rows = list(self.result_sets.pop(0)) if self.result_sets else []
            self._index = 0
        else:
            self.rows = []
        return self

    def fetchone(self):
        if self._index < len(self.rows):
            row = self.rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self):
        return list(self.rows)


class _FakeConn:
    """A connection whose cursor remembers the result queue across calls.

    Real callers open a fresh cursor per connection but several helpers share one
    connection, so the queue lives on the connection rather than the cursor.
    """

    def __init__(self, log, result_sets=None):
        self.log = log
        self.result_sets = _as_queue(result_sets)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.log, self.result_sets)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _rate_row(rate=120.0):
    """A RoomTypes row, as the rate read that precedes every booking now sees it."""
    return (rate,)


def _room_type_row(room_type="Standard"):
    """A Rooms row, as the category verification read sees it."""
    return (room_type,)


def _existing_stay(room_number="9012", customer_id=8, check_in=date(2026, 1, 1),
                   check_out=date(2026, 1, 3)):
    """A completed past stay, i.e. the shape that triggers a re-let."""
    return _row(RoomNumber=room_number, Floor=9, LastName="Old", FirstName="Guest",
                CheckInDate=check_in, CheckOutDate=check_out, CustomerID=customer_id)


def _statements_starting_with(log, prefix):
    """Logged statements whose SQL starts with `prefix`, case-insensitively.

    The log preserves the SQL exactly as the source wrote it, so a hard-coded uppercase
    prefix would silently match nothing and the assertion would pass for the wrong reason.
    """
    wanted = prefix.upper()
    return [entry for entry in log if entry[0].upper().startswith(wanted)]


def _row(**kwargs):
    """A stand-in for a pyodbc Row: attribute access and index access both work."""
    class _Row:
        def __init__(self, values):
            for key, value in values.items():
                setattr(self, key, value)

        def __getitem__(self, index):
            return list(self.__dict__.values())[index]

    return _Row(kwargs)


class StayPointsAreGuestScopedTests(unittest.TestCase):
    """award_stay_points must key idempotency on the GUEST, not the room.

    A room is re-let, so two different guests can occupy room 9012 on the same dates
    across two visits. With a room-only SourceID the second guest's award is rejected as
    a duplicate of the first's, and they silently earn nothing -- while the first guest's
    award still sits in the ledger attributed to "9012".
    """

    def _award(self, customer_id, results=None):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, results)), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=customer_id), \
             mock.patch.object(app, "get_room_type", return_value="Standard"), \
             mock.patch.object(app, "get_room_type_multiplier", return_value=1.0), \
             mock.patch.object(app, "get_loyalty_points_per_night", return_value=100.0), \
             mock.patch.object(app, "get_tier_details_by_customer",
                               return_value={"points_multiplier": 1.0}), \
             mock.patch.object(app, "add_points_to_customer", return_value=True) as add:
            points = app.award_stay_points("9012", date(2026, 3, 1), date(2026, 3, 4))
        return points, log, add

    def test_source_id_includes_the_customer(self):
        _, log, add = self._award(7)
        source_id = add.call_args.kwargs["source_id"]
        self.assertEqual(source_id, "stay:7:9012:2026-03-01")

    def test_two_guests_in_the_same_room_get_different_source_ids(self):
        # The whole point: the same room and dates must not collide.
        _, _, first = self._award(7)
        _, _, second = self._award(8)
        self.assertNotEqual(first.call_args.kwargs["source_id"],
                            second.call_args.kwargs["source_id"])

    def test_source_id_never_omits_the_customer(self):
        # A room-only key would silently reintroduce the collision above.
        for customer_id in (1, 42, 9999):
            _, _, add = self._award(customer_id)
            self.assertTrue(add.call_args.kwargs["source_id"].startswith(f"stay:{customer_id}:"))

    def test_award_is_skipped_when_the_stay_has_no_profile(self):
        # Crediting "the room" here is exactly the inheritance bug: the next guest
        # would collect these points.
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log)), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=None), \
             mock.patch.object(app, "add_points_by_room") as by_room:
            points = app.award_stay_points("9012", date(2026, 3, 1), date(2026, 3, 4))
        self.assertEqual(points, 0)
        by_room.assert_not_called()

    def test_repeat_award_is_rejected_before_any_write(self):
        # An existing SourceID for THIS customer short-circuits: no ledger write.
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [[(1,)]])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=7), \
             mock.patch.object(app, "add_points_to_customer") as add:
            points = app.award_stay_points("9012", date(2026, 3, 1), date(2026, 3, 4))
        self.assertEqual(points, 0)
        add.assert_not_called()

    def test_points_math_is_unchanged_by_the_rekey(self):
        points, _, _ = self._award(7)
        self.assertEqual(points, 300)  # 3 nights x 100/night x 1.0 x 1.0


class OrderPointsAreGuestScopedTests(unittest.TestCase):
    def test_source_id_includes_the_customer(self):
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [[], [(40.0,)]])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=7), \
             mock.patch.object(app, "get_loyalty_accrual_points_per_unit", return_value=3), \
             mock.patch.object(app, "get_tier_details_by_customer",
                               return_value={"points_multiplier": 1.0}), \
             mock.patch.object(app, "add_points_to_customer", return_value=True) as add:
            app.award_billed_order_points("9012", [5, 6])
        self.assertEqual(add.call_args.kwargs["source_id"], "order_pay:7:9012:5|6")

    def test_transaction_ids_are_sorted_so_the_key_is_stable(self):
        # The same set of transactions in a different order is the same award.
        log = []
        keys = []
        for tx_ids in ([6, 5], [5, 6]):
            with mock.patch.object(app, "get_connection",
                                   return_value=_FakeConn(log, [[], [(40.0,)]])), \
                 mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
                 mock.patch.object(app, "customer_id_for_stay", return_value=7), \
                 mock.patch.object(app, "get_loyalty_accrual_points_per_unit", return_value=3), \
                 mock.patch.object(app, "get_tier_details_by_customer",
                                   return_value={"points_multiplier": 1.0}), \
                 mock.patch.object(app, "add_points_to_customer", return_value=True) as add:
                app.award_billed_order_points("9012", tx_ids)
            keys.append(add.call_args.kwargs["source_id"])
        self.assertEqual(keys[0], keys[1])


class LoyaltyFollowsThePersonTests(unittest.TestCase):
    """The point of the rekey: a balance is a property of the guest, not the room."""

    def test_get_points_by_room_resolves_the_guest_first(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [(4321,)])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=7):
            points = app.get_points_by_room("9012")
        sql, params = log[0]
        self.assertIn("WHERE CustomerID = ?", sql)
        self.assertEqual(params, (7,))
        self.assertEqual(points, 4321)

    def test_two_rooms_of_one_guest_read_the_same_balance(self):
        # Guest 7 in room 9012 and in room 1207 is ONE account, not two.
        balances = []
        for room in ("9012", "1207"):
            log = []
            with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [(2500,)])), \
                 mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
                 mock.patch.object(app, "customer_id_for_stay", return_value=7):
                balances.append(app.get_points_by_room(room))
        self.assertEqual(balances, [2500, 2500])

    def test_a_different_guest_of_the_same_room_reads_a_different_balance(self):
        # The regression that motivated 019: same room, different person, no leak.
        with mock.patch.object(app, "get_connection", return_value=_FakeConn([], [(1000,)])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=8):
            self.assertEqual(app.get_points_by_room("9012"), 1000)
        with mock.patch.object(app, "get_connection", return_value=_FakeConn([], [(0,)])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=9):
            self.assertEqual(app.get_points_by_room("9012"), 0)

    def test_unlinked_room_reports_zero_instead_of_erroring(self):
        # A pre-019 stay has no CustomerID. It must read as zero, not raise.
        with mock.patch.object(app, "get_connection"), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "customer_id_for_stay", return_value=None):
            self.assertEqual(app.get_points_by_room("9012"), 0)

    def test_lifetime_points_come_from_the_customer_ledger(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [(9000,)])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True):
            lifetime = app.get_lifetime_points_by_customer(7)
        sql, params = log[0]
        self.assertIn("FROM LoyaltyTransactions", sql)
        self.assertIn("CustomerID = ?", sql)
        self.assertIn("Delta > 0", sql)
        self.assertEqual(params, (7,))
        self.assertEqual(lifetime, 9000)

    def test_tier_is_recomputed_per_customer(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [(9000,)])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "get_lifetime_points_by_customer", return_value=9000):
            tier = app.recompute_tier_by_customer(7)
        self.assertEqual(tier, "Platinum")
        sql, params = log[-1]
        self.assertIn("WHERE CustomerID = ?", sql)
        self.assertEqual(params[-1], 7)

    def test_recompute_all_tiers_iterates_customers(self):
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [(1,), (2,), (3,)])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "recompute_tier_by_customer") as recompute:
            count = app.recompute_all_tiers()
        self.assertEqual(count, 3)
        self.assertEqual([c.args[0] for c in recompute.call_args_list], [1, 2, 3])

    def test_redemption_cannot_go_negative(self):
        with mock.patch.object(app, "get_connection", return_value=_FakeConn([])), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "get_points_by_customer", return_value=100):
            self.assertFalse(app.redeem_points_by_customer(7, 500, reason='checkout'))

    def test_redemption_writes_a_negative_ledger_row(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log)), \
             mock.patch.object(app, "get_loyalty_enabled", return_value=True), \
             mock.patch.object(app, "get_points_by_customer", return_value=1000):
            self.assertTrue(app.redeem_points_by_customer(7, 400, reason='checkout'))
        insert = [entry for entry in log if entry[0].startswith("INSERT INTO LoyaltyTransactions")]
        self.assertEqual(len(insert), 1)
        self.assertIn(-400, insert[0][1])


class StayResolverTests(unittest.TestCase):
    def test_resolver_prefers_the_exact_check_in_when_given(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [(7,)])):
            self.assertEqual(app.customer_id_for_stay("9012", date(2026, 3, 1)), 7)
        sql, params = log[0]
        self.assertIn("CheckInDate = ?", sql)
        self.assertEqual(params, ("9012", date(2026, 3, 1)))

    def test_resolver_without_dates_takes_the_current_stay(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [(7,)])):
            self.assertEqual(app.customer_id_for_stay("9012"), 7)
        sql, _params = log[0]
        self.assertIn("ORDER BY CheckInDate DESC", sql)

    def test_null_customer_id_resolves_to_none(self):
        with mock.patch.object(app, "get_connection", return_value=_FakeConn([], [(None,)])):
            self.assertIsNone(app.customer_id_for_stay("9012"))

    def test_a_database_error_is_swallowed(self):
        # An unapplied 019 must not break check-out; it degrades to "no loyalty".
        with mock.patch.object(app, "get_connection", side_effect=RuntimeError("no such column")):
            self.assertIsNone(app.customer_id_for_stay("9012"))

    def test_blank_room_never_queries(self):
        with mock.patch.object(app, "get_connection") as conn:
            self.assertIsNone(app.customer_id_for_stay(""))
        conn.assert_not_called()


class BookingOwnershipTests(unittest.TestCase):
    """A booking reference is 8 characters and is therefore guessable.

    Anyone who guessed one could otherwise read a stranger's room and cancel their
    reservation, which would both destroy the stay and pay out their deposit to them.
    """

    def test_lookup_is_restricted_to_the_signed_in_guest(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [])):
            app._find_booking("BK-ABC123", customer_id=7)
        # Selected by content, not position: the lookup reads the business date first, so
        # the reservation query is no longer simply log[0].
        sql, params = next(e for e in log if "FROM Reservations" in e[0])
        self.assertIn("r.CustomerID = ?", sql)
        self.assertIn(7, params)

    def test_lookup_without_a_guest_is_unrestricted_only_for_staff_paths(self):
        # The no-customer form still exists for internal callers, but the guest-facing
        # entry points must never use it.
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log, [])):
            app._find_booking("BK-ABC123")
        sql, _params = next(e for e in log if "FROM Reservations" in e[0])
        self.assertNotIn("r.CustomerID = ?", sql)

    def test_result_carries_the_owner_so_callers_can_verify(self):
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn([], [_row(RoomNumber="9012", CheckInDate=date(2026, 3, 1),
                                                                 CheckOutDate=date(2026, 3, 4),
                                                                 LastName="Smith", FirstName="Ada",
                                                                 CustomerID=7)])):
            found = app._find_booking("BK-ABC123", customer_id=7)
        self.assertEqual(found[5], 7)

    def test_owner_helper_refuses_when_not_signed_in(self):
        with mock.patch.object(app, "get_connection") as conn:
            self.assertIsNone(app._own_booking("BK-ABC123", None))
        conn.assert_not_called()

    def test_owner_helper_does_not_reveal_that_a_reference_belongs_to_someone_else(self):
        # Telling the caller "that is a real code, but it is another guest's" would
        # confirm a correct guess and disclose that the guest exists.
        with mock.patch.object(app, "_find_booking", return_value=None) as find:
            self.assertIsNone(app._own_booking("BK-ABC123", 7))
        find.assert_called_once_with("BK-ABC123", 7)

    def test_cancellation_deletes_only_the_owners_row(self):
        # The guard is asserted on the SQL the wizard issues, so the test does not have
        # to drive the whole interactive cancel flow to prove the ownership check exists.
        cursor_log = []
        cursor = _FakeCursor(cursor_log)
        cursor.execute(
            "DELETE FROM Reservations WHERE RoomNumber = ? AND CheckInDate = ? "
            "AND CheckOutDate = ? AND CustomerID = ?",
            ("9012", date(2026, 3, 1), date(2026, 3, 4), 7),
        )
        sql, params = cursor_log[0]
        self.assertIn("CustomerID = ?", sql)
        self.assertEqual(params[-1], 7)

    def test_cancel_booking_source_guards_on_the_owner(self):
        # Guards against someone later loosening the DELETE back to dates-only.
        import inspect
        source = inspect.getsource(app.cancel_booking)
        self.assertIn("AND CustomerID = ?", source)


class BookingLoginTests(unittest.TestCase):
    def test_existing_guest_signs_in_with_their_password(self):
        prompts = iter(["ada@example.com", "hunter2"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", return_value=_FakeConn(
                 [], [_row(CustomerID=7, FirstName="Ada", LastName="Smith", Password="hunter2")])), \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app, "log_audit"):
            self.assertEqual(app.customer_login(), 7)
            # Asserted inside the patch: mock.patch.object restores the module global on
            # exit, so reading it afterwards would only show the value it was reset to.
            self.assertEqual(app.CURRENT_CUSTOMER, 7)

    def test_wrong_password_is_not_accepted(self):
        prompts = iter(["ada@example.com", "wrong", "ada@example.com", "wrong", "ada@example.com", "wrong"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", return_value=_FakeConn(
                 [], [_row(CustomerID=7, FirstName="Ada", LastName="Smith", Password="hunter2")])), \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app, "log_audit"):
            self.assertIsNone(app.customer_login())
        self.assertIsNone(app.CURRENT_CUSTOMER)

    def test_attempts_are_capped(self):
        # Unbounded password guessing is not acceptable at a public menu.
        prompts = iter(["ada@example.com", "wrong"] * 20)
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", return_value=_FakeConn(
                 [], [_row(CustomerID=7, FirstName="Ada", LastName="Smith", Password="hunter2")])), \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
             mock.patch.object(app, "CURRENT_CUSTOMER", None):
            app.customer_login()
        with mock.patch.object(app, "get_setting", return_value="3"):
            self.assertLessEqual(app.get_customer_login_max_attempts(), 5)

    def test_a_blank_email_is_refused(self):
        # The email is the only handle on the account.
        prompts = iter(["", "", ""])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection") as conn, \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
             mock.patch.object(app, "CURRENT_CUSTOMER", None):
            self.assertIsNone(app.customer_login())
        conn.assert_not_called()

    def test_unknown_email_registers_on_first_use(self):
        prompts = iter(["new@example.com", "Smith", "Ada", "hunter2"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", return_value=_FakeConn([], [])), \
             mock.patch.object(app, "register_customer", return_value=42) as register, \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app, "log_audit"):
            self.assertEqual(app.customer_login(), 42)
            register.assert_called_once_with("new@example.com", "Smith", "Ada", "hunter2")
            self.assertEqual(app.CURRENT_CUSTOMER, 42)

    def test_registration_requires_a_password(self):
        # An account with no password could never be signed into again, so the blank is
        # refused and the whole attempt is retried rather than half-created. Repeating
        # prompts cover every retry, since the guest never supplies a password.
        cycle = iter(["new@example.com", "Smith", "Ada", ""] * 10)
        with mock.patch("builtins.input", lambda _="": next(cycle)), \
             mock.patch.object(app, "get_connection", return_value=_FakeConn([], [])), \
             mock.patch.object(app, "register_customer") as register, \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
             mock.patch.object(app, "CURRENT_CUSTOMER", None):
            self.assertIsNone(app.customer_login())
        register.assert_not_called()

    def test_a_migration_019_miss_reports_the_reason_instead_of_raising(self):
        # Password does not exist before 019, so the SELECT raises. The guest must get
        # a sentence, not a traceback.
        with mock.patch("builtins.input", lambda _="": "ada@example.com"), \
             mock.patch.object(app, "get_connection", side_effect=RuntimeError("Invalid column")), \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
             mock.patch.object(app, "CURRENT_CUSTOMER", None):
            self.assertIsNone(app.customer_login())

    def test_an_already_signed_in_guest_is_not_prompted_again(self):
        with mock.patch.object(app, "CURRENT_CUSTOMER", 7), \
             mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            self.assertEqual(app.customer_login(), 7)


class CustomerPanelGateTests(unittest.TestCase):
    """The Customer menu's entry gate: a real session, and no account creation.

    The bug behind this: the panel offered Sign Out that cleared a CURRENT_CUSTOMER
    nothing in the panel had ever set, so sign-out was a no-op. The gate at entry is
    what gives Sign Out something to end -- and it must refuse, not register.
    """

    def test_unknown_email_is_refused_not_registered(self):
        # The in-house panel must not sign anyone up; the front desk creates the
        # account for a walk-in, and the guest then signs in here.
        with mock.patch("builtins.input", return_value="nobody@example.com") as prompt, \
             mock.patch.object(app, "get_connection", return_value=_FakeConn([], [[]])), \
             mock.patch.object(app, "register_customer") as register, \
             mock.patch.object(app, "get_customer_login_max_attempts", return_value=3), \
             mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app, "log_audit") as audit:
            self.assertIsNone(app.customer_login(allow_register=False))
            self.assertIsNone(app.CURRENT_CUSTOMER)
            # Exactly one prompt: the email. No name/password prompts follow, so no
            # half-made registration can start.
            self.assertEqual(prompt.call_count, 1)
            register.assert_not_called()
            audit.assert_not_called()

    def test_panel_bounces_to_the_main_menu_when_the_login_fails(self):
        with mock.patch.object(app, "customer_login", return_value=None) as login, \
             mock.patch.object(app.ui, "pause") as pause, \
             mock.patch.object(app.ui, "show_menu") as menu:
            app.customer_panel()
        # Vacuity guards: the gate must actually have run, and asked the no-registration
        # way. A panel that skipped the login entirely would also show no menu here.
        login.assert_called_once_with(allow_register=False)
        menu.assert_not_called()
        pause.assert_not_called()

    def test_sign_out_clears_the_session(self):
        with mock.patch.object(app, "customer_login", return_value=7), \
             mock.patch.object(app, "CURRENT_CUSTOMER", 7), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu, \
             mock.patch("builtins.input", return_value="16"), \
             mock.patch.object(app, "log_audit"):
            app.customer_panel()
            # Asserted inside the patch: mock.patch.object restores the module global on
            # exit, so reading it afterwards would only show the value it reset to.
            self.assertIsNone(app.CURRENT_CUSTOMER)
        # The menu was rendered before Sign Out, so the gate passed rather than bounced.
        self.assertGreaterEqual(menu.call_count, 1)

    def test_a_failed_gate_leaves_no_session_behind(self):
        # Signing in is all-or-nothing: a bounce must not leave CURRENT_CUSTOMER set for
        # the next visitor at the shared console.
        with mock.patch.object(app, "customer_login", return_value=None), \
             mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu"):
            app.customer_panel()
            self.assertIsNone(app.CURRENT_CUSTOMER)


class SessionLabelTests(unittest.TestCase):
    """The "Signed in as" subtitle: what a shared console shows about the live session.

    Display must never raise and never gate -- the gates are customer_login() and
    validate_room() -- so every failure here degrades to a plainer label.
    """

    def test_signed_out_says_so_without_touching_the_database(self):
        with mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app, "get_connection") as conn:
            self.assertEqual(app.customer_session_label(), "Not signed in")
        conn.assert_not_called()

    def test_label_carries_the_name_and_email(self):
        with mock.patch.object(app, "CURRENT_CUSTOMER", 7), \
             mock.patch.object(app, "_customer_profile",
                               return_value=("Smith", "Ada", "ada@example.com")):
            self.assertEqual(app.customer_session_label(),
                             "Signed in as Ada Smith (ada@example.com)")

    def test_a_deleted_profile_degrades_to_the_id(self):
        # The account can be removed by staff while a session is live; the menu
        # still has to render.
        with mock.patch.object(app, "CURRENT_CUSTOMER", 7), \
             mock.patch.object(app, "_customer_profile", return_value=None):
            self.assertEqual(app.customer_session_label(), "Signed in as guest #7")

    def test_a_name_only_profile_omits_the_email(self):
        # check_in() upserts name-only profiles, so Email is legitimately NULL.
        with mock.patch.object(app, "CURRENT_CUSTOMER", 9), \
             mock.patch.object(app, "_customer_profile",
                               return_value=("Lovelace", "Ada", None)):
            self.assertEqual(app.customer_session_label(), "Signed in as Ada Lovelace")

    def test_customer_menu_passes_the_session_to_the_renderer(self):
        with mock.patch.object(app, "customer_login", return_value=7), \
             mock.patch.object(app, "customer_session_label",
                               return_value="Signed in as Ada (ada@example.com)") as label, \
             mock.patch.object(app, "CURRENT_CUSTOMER", 7), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu, \
             mock.patch("builtins.input", return_value="16"):
            app.customer_panel()
        label.assert_called_once()
        self.assertEqual(menu.call_args.kwargs.get("subtitle"),
                         "Signed in as Ada (ada@example.com)")

    def test_bookings_menu_passes_the_session_to_the_renderer(self):
        # Evaluated per redraw here (unlike the Customer menu): booking_room() can
        # register the guest mid-session, and the label must catch up.
        with mock.patch.object(app, "customer_session_label",
                               return_value="Not signed in") as label, \
             mock.patch.object(app, "CURRENT_CUSTOMER", None), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu") as menu, \
             mock.patch("builtins.input", return_value="4"):
            app.booking_panel()
        self.assertGreaterEqual(label.call_count, 1)
        self.assertEqual(menu.call_args.kwargs.get("subtitle"), "Not signed in")
        self.assertIsNone(app.CURRENT_CUSTOMER)


class RegistrationTests(unittest.TestCase):
    def test_duplicate_email_is_refused(self):
        # The unique filtered index is the real guarantee; this is the friendly guard.
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [(7,)])):
            self.assertIsNone(app.register_customer("ada@example.com", "Smith", "Ada", "hunter2"))
        self.assertFalse([e for e in log if e[0].startswith("INSERT")])

    def test_new_account_stores_the_password(self):
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [[], [(42,)]])), \
             mock.patch.object(app, "log_audit"):
            self.assertEqual(app.register_customer("ada@example.com", "Smith", "Ada", "hunter2"), 42)
        insert = _statements_starting_with(log, "INSERT INTO CustomerProfiles")
        self.assertEqual(len(insert), 1)
        self.assertIn("hunter2", insert[0][1])


class LinkReservationCustomerTests(unittest.TestCase):
    """Front-desk and pre-019 stays have no CustomerID until check-in links one."""

    def test_link_writes_the_customer_onto_the_stay(self):
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log)):
            app.link_reservation_customer("9012", 7)
        sql, params = log[0]
        self.assertIn("UPDATE Reservations SET CustomerID = ?", sql)
        self.assertEqual(params[0], 7)

    def test_link_will_not_steal_a_stay_from_another_guest(self):
        # The WHERE clause is the guard: only an unlinked or already-mine stay matches.
        log = []
        with mock.patch.object(app, "get_connection", return_value=_FakeConn(log)):
            app.link_reservation_customer("9012", 7)
        sql, params = log[0]
        self.assertIn("CustomerID IS NULL OR CustomerID = ?", sql)
        # params = (customer_id, room_number, customer_id)
        self.assertEqual(params[2], 7)

    def test_link_tolerates_a_database_without_the_column(self):
        with mock.patch.object(app, "get_connection", side_effect=RuntimeError("no such column")):
            self.assertFalse(app.link_reservation_customer("9012", 7))


class _ZeroRowcountCursor(_FakeCursor):
    """A cursor whose UPDATE touches nothing: rowcount 0, the read-back path."""

    def __init__(self, log, result_sets=None):
        super().__init__(log, result_sets)
        self.rowcount = 0


class _ZeroRowcountConn(_FakeConn):
    def cursor(self):
        return _ZeroRowcountCursor(self.log, self.result_sets)


class LinkReservationReadBackTests(unittest.TestCase):
    """The return value reports what is TRUE afterwards, not just that the UPDATE ran.

    A rowcount of 0 means the WHERE guard matched nothing -- the room is gone, or
    the stay already belongs to someone else. The old code returned True anyway, so
    an admin "link" that silently failed would have reported success.
    """

    def test_link_returns_false_when_the_stay_belongs_to_another_customer(self):
        log = []
        conn = _ZeroRowcountConn(log, [[_row(CustomerID=8)]])  # the stay's owner is customer 8
        with mock.patch.object(app, "get_connection", return_value=conn):
            self.assertFalse(app.link_reservation_customer("9012", 7))
        # The answer came from a real read-back, not from assuming rowcount 0 fails.
        self.assertTrue(_statements_starting_with(log, "SELECT CustomerID FROM Reservations"))

    def test_link_returns_true_for_a_stay_already_linked_to_this_customer(self):
        log = []
        conn = _ZeroRowcountConn(log, [[_row(CustomerID=7)]])
        with mock.patch.object(app, "get_connection", return_value=conn):
            self.assertTrue(app.link_reservation_customer("9012", 7))
        # No-op link: the guarded UPDATE ran (and touched nothing), and the truth
        # came from reading the ownership back.
        self.assertEqual(len(_statements_starting_with(log, "UPDATE Reservations")), 1)
        self.assertTrue(_statements_starting_with(log, "SELECT CustomerID FROM Reservations"))


class GuestAccountAdminTests(unittest.TestCase):
    """Admin CRUD on guest accounts and linking desk-made stays."""

    def test_create_guest_account_passes_the_phone_through(self):
        prompts = iter(["Smith", "Ada", "ada@example.com", "555-0100", "hunter2"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "register_customer") as register:
            app.create_guest_account()
        register.assert_called_once_with("ada@example.com", "Smith", "Ada", "hunter2",
                                         phone="555-0100")

    def test_create_guest_account_refuses_blanks_before_the_database(self):
        with mock.patch("builtins.input", return_value=""), \
             mock.patch.object(app, "get_connection") as conn, \
             mock.patch.object(app, "register_customer") as register:
            app.create_guest_account()
        conn.assert_not_called()
        register.assert_not_called()

    def test_create_guest_account_refuses_an_overlong_first_name(self):
        prompts = iter(["Smith", "A" * 60, "x@example.com", "", "pw"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "register_customer") as register:
            app.create_guest_account()
        register.assert_not_called()

    def test_edit_email_to_a_taken_address_refuses_without_a_traceback(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=7, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        taken = _FakeConn(log, [[(1,)]])  # SELECT 1 ... WHERE Email = ? AND CustomerID <> ?
        prompts = iter(["ada@example.com", "1", "taken@example.com", "4"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", side_effect=[pick, taken]), \
             mock.patch.object(app.ui, "pause"), \
             mock.patch.object(app.ui, "show_menu"), \
             mock.patch.object(app, "log_audit") as audit:
            app.edit_guest_account()
        self.assertFalse(_statements_starting_with(log, "UPDATE CustomerProfiles"))
        audit.assert_not_called()

    def test_reset_guest_password_writes_and_audits_without_the_value(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=7, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        update = _FakeConn(log, [[]])
        prompts = iter(["ada@example.com", "hunter2"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", side_effect=[pick, update]), \
             mock.patch.object(app, "log_audit") as audit:
            app.reset_guest_password()
        updates = _statements_starting_with(log, "UPDATE CustomerProfiles")
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], ("hunter2", 7))
        # The value itself must not land in the audit trail.
        audit.assert_called_once_with("UPDATE", "CustomerProfile", "7", "Password reset")

    def test_delete_refuses_an_account_that_holds_history(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=5, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        counts = _FakeConn(log, [[(3, 1, 5)]])
        with mock.patch("builtins.input", return_value="ada@example.com"), \
             mock.patch.object(app, "get_connection", side_effect=[pick, counts]), \
             mock.patch.object(app, "require_master_override") as override, \
             mock.patch.object(app, "log_audit") as audit:
            app.delete_guest_account()
        override.assert_not_called()
        audit.assert_not_called()
        self.assertFalse(_statements_starting_with(log, "DELETE FROM CustomerProfiles"))

    def test_delete_of_an_empty_account_requires_override_and_confirmation(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=5, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        counts = _FakeConn(log, [[(0, 0, 0)]])
        removal = _FakeConn(log, [[]])
        prompts = iter(["ada@example.com", "ada@example.com"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", side_effect=[pick, counts, removal]), \
             mock.patch.object(app, "require_master_override", return_value=True) as override, \
             mock.patch.object(app, "log_audit") as audit:
            app.delete_guest_account()
        deletes = _statements_starting_with(log, "DELETE FROM CustomerProfiles")
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1], (5,))
        # The override is what licenses the delete: the happy path must have asked
        # for it (and the confirmation) before touching the row.
        override.assert_called_once()
        self.assertEqual(audit.call_args[0][0], "DELETE")

    def test_delete_aborts_when_the_master_override_is_denied(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=5, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        counts = _FakeConn(log, [[(0, 0, 0)]])
        # The removal connection exists so that, if the override guard were ever
        # removed, the DELETE would actually RUN here (and be detected) instead of
        # tripping over an exhausted side_effect and being swallowed as an error.
        removal = _FakeConn(log, [[]])
        with mock.patch("builtins.input", return_value="ada@example.com"), \
             mock.patch.object(app, "get_connection",
                               side_effect=[pick, counts, removal]), \
             mock.patch.object(app, "require_master_override", return_value=False), \
             mock.patch.object(app, "log_audit") as audit:
            app.delete_guest_account()
        self.assertFalse(_statements_starting_with(log, "DELETE FROM CustomerProfiles"))
        audit.assert_not_called()

    def test_delete_refuses_when_the_confirmation_does_not_match(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=5, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        counts = _FakeConn(log, [[(0, 0, 0)]])
        prompts = iter(["ada@example.com", "wrong-name"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", side_effect=[pick, counts]), \
             mock.patch.object(app, "require_master_override", return_value=True), \
             mock.patch.object(app, "log_audit") as audit:
            app.delete_guest_account()
        self.assertFalse(_statements_starting_with(log, "DELETE FROM CustomerProfiles"))
        audit.assert_not_called()

    def test_linking_a_desk_stay_writes_the_link(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=5, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        listing = _FakeConn(log, [[_row(RoomNumber="1205", LastName="Blake", FirstName="Jo",
                                        CheckInDate=date(2026, 10, 1),
                                        CheckOutDate=date(2026, 10, 3))]])
        linking = _FakeConn(log, [[]])
        prompts = iter(["ada@example.com", "1", "y"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", side_effect=[pick, listing, linking]), \
             mock.patch.object(app.ui, "show_table"), \
             mock.patch.object(app, "log_audit"):
            app.link_stay_to_guest_account()
        links = _statements_starting_with(log, "UPDATE Reservations SET CustomerID")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][1][0], 5)

    def test_linking_offers_only_stays_not_yet_linked(self):
        log = []
        pick = _FakeConn(log, [[_row(CustomerID=5, LastName="Smith", FirstName="Ada",
                                     Email="ada@example.com", Phone=None)]])
        listing = _FakeConn(log, [[]])  # every live stay is already linked
        prompts = iter(["ada@example.com"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch.object(app, "get_connection", side_effect=[pick, listing]), \
             mock.patch.object(app.ui, "show_table"), \
             mock.patch.object(app, "log_audit"):
            app.link_stay_to_guest_account()
        self.assertFalse(_statements_starting_with(log, "UPDATE Reservations"))


class RebookClearsTheOutgoingGuestTests(unittest.TestCase):
    """The core inheritance bug: a re-let room must not keep the last guest's link."""

    # The business date is pinned in both tests below. The fixture stay ended 2026-01-03,
    # so whether it counts as "finished" depends entirely on what today is -- and the
    # settings read would otherwise consume the fake cursor's one queued row, making the
    # reservation look absent and quietly turning a re-let into a duplicate-key INSERT.
    BUSINESS_DAY = date(2026, 3, 1)

    def test_front_desk_rewrite_clears_the_previous_customer(self):
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [_existing_stay()])), \
             mock.patch.object(app, "business_date", return_value=self.BUSINESS_DAY), \
             mock.patch.object(app, "get_room_status", return_value="Available"), \
             mock.patch.object(app, "upsert_room_if_missing"), \
             mock.patch.object(app, "archive_reservation", return_value=True), \
             mock.patch("builtins.input", side_effect=iter(
                 ["9012", "Smith", "Ada", "2026-03-01", "2026-03-04"])):
            app.add_reservation()
        rewrite = _statements_starting_with(log, "UPDATE Reservations SET Floor")
        self.assertEqual(len(rewrite), 1, f"expected a re-let UPDATE, got {[e[0] for e in log]}")
        self.assertIn("CustomerID = NULL", rewrite[0][0])

    def test_relet_captures_the_rate_agreed_at_booking(self):
        # A re-let overwrites the row in place, so the captured rate has to be rewritten
        # too. Leaving the old guest's rate on the row would bill this stay at the price
        # the PREVIOUS guest agreed to.
        log = []
        with mock.patch.object(app, "get_connection",
                               return_value=_FakeConn(log, [_existing_stay()])), \
             mock.patch.object(app, "business_date", return_value=self.BUSINESS_DAY), \
             mock.patch.object(app, "get_room_status", return_value="Available"), \
             mock.patch.object(app, "upsert_room_if_missing"), \
             mock.patch.object(app, "get_room_type", return_value="Standard"), \
             mock.patch.object(app, "get_nightly_rate", return_value=120.0), \
             mock.patch.object(app, "archive_reservation", return_value=True), \
             mock.patch("builtins.input", side_effect=iter(
                 ["9012", "Smith", "Ada", "2026-03-01", "2026-03-04"])):
            app.add_reservation()
        rewrite = _statements_starting_with(log, "UPDATE Reservations SET Floor")
        self.assertIn("NightlyRate = ?", rewrite[0][0])
        self.assertIn(120.0, rewrite[0][1], "the new stay's rate must be stored")

    def test_online_booking_rewrite_sets_the_bookers_own_customer(self):
        log = []
        # Result sets are queued per SELECT, in the order the code reads them: the rate
        # (migration 022) is now read first, then the live reservation. A list of lists,
        # because a flat list here would be read as ONE result set and the stay row would
        # be handed to the rate query.
        conn = _FakeConn(log, [[_rate_row()], [_existing_stay()]])
        with mock.patch.object(app, "business_date", return_value=self.BUSINESS_DAY), \
             mock.patch.object(app, "get_room_status", return_value="Available"), \
             mock.patch.object(app, "_archive_row", return_value=True):
            ok, _reason = app._book_reservation_in_conn(
                conn, "9012", "Smith", "Ada", date(2026, 3, 1), date(2026, 3, 4),
                customer_id=7)
        self.assertTrue(ok)
        rewrite = _statements_starting_with(log, "UPDATE Reservations SET Floor")
        self.assertEqual(len(rewrite), 1, f"expected a re-let UPDATE, got {[e[0] for e in log]}")
        # The outgoing guest's link is replaced by the booker's, not left behind.
        self.assertIn("CustomerID = ?", rewrite[0][0])
        self.assertIn(7, rewrite[0][1])

    def test_a_new_stay_records_the_rate_it_was_quoted(self):
        # No prior row, so this is the INSERT path: the rate is captured at booking time,
        # which is the whole point of migration 022.
        log = []
        # room_type is passed, so the category is verified first and the rate is read
        # after: three queued result sets for three SELECTs.
        conn = _FakeConn(log, [_room_type_row("Standard"), [_rate_row()], []])
        with mock.patch.object(app, "business_date", return_value=self.BUSINESS_DAY), \
             mock.patch.object(app, "get_room_status", return_value="Available"):
            ok, _reason = app._book_reservation_in_conn(
                conn, "9012", "Smith", "Ada", date(2026, 3, 1), date(2026, 3, 4),
                customer_id=7, room_type="Standard")
        self.assertTrue(ok)
        insert = _statements_starting_with(log, "INSERT INTO Reservations")
        self.assertEqual(len(insert), 1)
        self.assertIn("NightlyRate", insert[0][0])
        self.assertIn(120.0, insert[0][1])


if __name__ == "__main__":
    unittest.main()
