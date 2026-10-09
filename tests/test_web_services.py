# type: ignore
"""Unit tests for the Phase-2 web-facing services (PLAN-web-api.md).

The pilot slice's console flows (staff login, guest login, create-booking, check-in)
were rewritten as thin adapters over non-interactive service twins so the FastAPI
edition can call the same business rules. These tests pin the service contracts -- the
status dicts, the lockout bookkeeping that must live in one place, the actor plumbing
through to log_audit, and the card-last4 parameter that frees a payment row from the
process global.

Everything here is a thin DB wrapper or pure logic, so the whole file runs without
SQL Server.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
import session
from patch_main import patch_main


def _row(**kwargs):
    """A stand-in for a pyodbc Row: attribute access and index access both work."""
    class _Row:
        def __init__(self, values):
            for key, value in values.items():
                setattr(self, key, value)

        def __getitem__(self, index):
            return list(self.__dict__.values())[index]

    return _Row(kwargs)


class _NoConn:
    """A get_connection() result that yields None -- the real 'connect failed' shape."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class _ScriptedCursor:
    """Answers each SELECT from a (SQL fragment, rows) script, logging every statement.

    Unlike a queue-based fake, the answer comes from what the statement SAYS, so a
    service that reads the same table twice gets the same answer without the test having
    to sequence the queue. A script entry may also be a callable `params -> rows`, for
    the rare case where two identical-shaped queries must answer differently.
    """

    def __init__(self, log, scripts):
        self.log = log
        self.scripts = scripts
        self.rows = []
        self.rowcount = -1

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()).upper(), params))
        upper = sql.upper()
        self.rows = []
        self.rowcount = -1
        if upper.startswith(("SELECT", "WITH")) or "OUTPUT INSERTED" in upper:
            for fragment, answer in self.scripts:
                if fragment.upper() in upper:
                    self.rows = list(answer(params)) if callable(answer) else list(answer)
                    break
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _ScriptedConn:
    def __init__(self, log, scripts=None):
        self.log = log
        self.scripts = list(scripts or [])
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _ScriptedCursor(self.log, self.scripts)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _TxnConn:
    """Minimal connection for create_booking(): the DB helpers are patched, so only the
    commit/rollback discipline is observed."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _PaymentConn:
    """For record_booking_payment(): the INSERT..OUTPUT returns a PaymentID, and a
    SELECT TOP 1 CardLast4 draws the original charge's card from a queue."""

    def __init__(self, log, cards=None):
        self.log = log
        self.cards = list(cards or [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _PaymentCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class _PaymentCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql, params=()):
        self.log = self.conn.log
        self.log.append((" ".join(sql.split()), params))
        upper = sql.upper()
        self.rows = []
        if "OUTPUT INSERTED.PAYMENTID" in upper:
            self.rows = [(77,)]
        elif upper.startswith("SELECT TOP 1 CARDLAST4"):
            # _original_charge_card() pops the original charge's card.
            self.rows = [self.conn.cards.pop(0)] if self.conn.cards else []
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


_STAFF_OK = _row(Password="pw", FailedAttempts=3, LockoutTime=None, Role="admin")


class StaffLoginServiceTests(unittest.TestCase):
    """verify_staff_login(): the credential check and lockout bookkeeping the console
    adapter and the web path share."""

    def _login(self, row, username="bob", password="pw", threshold=None, duration=None):
        log = []
        conn = _ScriptedConn(log, [("FROM USERS", [row] if row is not None else [])])
        with ExitStack() as stack:
            stack.enter_context(patch_main("get_connection", return_value=conn))
            audit = stack.enter_context(patch_main("log_audit"))
            if threshold is not None:
                stack.enter_context(patch_main("get_lockout_threshold", return_value=threshold))
            if duration is not None:
                stack.enter_context(patch_main("get_lockout_duration", return_value=duration))
            result = app.verify_staff_login(username, password)
        return result, log, audit

    def test_ok_resets_counters_and_audits_with_the_actor(self):
        result, log, audit = self._login(_STAFF_OK)
        self.assertEqual(result, {"status": "ok", "role": "admin"})
        audit.assert_called_once_with("LOGIN", "User", "bob", "Role admin", user="bob")
        updates = [sql for sql, _ in log if sql.startswith("UPDATE USERS")]
        self.assertEqual(
            updates,
            ["UPDATE USERS SET FAILEDATTEMPTS = 0, LOCKOUTTIME = NULL WHERE USERNAME = ?"])

    def test_ok_never_touches_the_process_global(self):
        with patch_main("CURRENT_USER", "whoever"), \
             patch_main("get_connection",
                        return_value=_ScriptedConn([], [("FROM USERS", [_STAFF_OK])])), \
             patch_main("log_audit"):
            result = app.verify_staff_login("bob", "pw")
            # Asserted inside the patch: mock restores the global on exit.
            self.assertEqual(app.CURRENT_USER, "whoever")
        self.assertEqual(result["status"], "ok")

    def test_unknown_user_is_not_a_login(self):
        result, log, audit = self._login(None)
        self.assertEqual(result, {"status": "not_found"})
        audit.assert_not_called()
        self.assertFalse(any(sql.startswith("UPDATE") for sql, _ in log))

    def test_a_locked_account_is_refused_even_with_the_right_password(self):
        locked = _row(Password="pw", FailedAttempts=1,
                      LockoutTime=datetime.now() + timedelta(days=1), Role="admin")
        result, log, audit = self._login(locked, password="pw")
        self.assertEqual(result["status"], "locked")
        self.assertIsInstance(result["lockout_time"], datetime)
        audit.assert_not_called()
        self.assertFalse(any(sql.startswith("UPDATE") for sql, _ in log))

    def test_bad_password_increments_the_counter_and_audits(self):
        row = _row(Password="pw", FailedAttempts=0, LockoutTime=None, Role="staff")
        result, log, audit = self._login(row, password="wrong", threshold=3)
        self.assertEqual(result, {"status": "bad_password", "attempts": 1, "role": "staff"})
        audit.assert_called_once_with(
            "LOGIN_FAILED", "User", "bob", "Failed attempt 1/3", user="bob")
        updates = [sql for sql, _ in log if sql.startswith("UPDATE USERS")]
        self.assertEqual(updates, ["UPDATE USERS SET FAILEDATTEMPTS = ? WHERE USERNAME = ?"])

    def test_threshold_reached_locks_the_account(self):
        row = _row(Password="pw", FailedAttempts=2, LockoutTime=None, Role="manager")
        result, log, audit = self._login(row, password="wrong", threshold=3, duration=30)
        self.assertEqual(result["status"], "lockout")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["role"], "manager")
        self.assertIsInstance(result["lockout_time"], datetime)
        self.assertEqual([c.args[0] for c in audit.call_args_list],
                         ["LOGIN_FAILED", "LOCKOUT"])
        self.assertEqual(audit.call_args_list[1].kwargs["user"], "bob")
        # The lockout write sets both the counter and the timestamp.
        self.assertTrue(any("LOCKOUTTIME" in sql for sql, _ in log))

    def test_unavailable_when_the_database_is_down(self):
        with patch_main("get_connection", return_value=_NoConn()):
            result = app.verify_staff_login("bob", "pw")
        self.assertEqual(result, {"status": "unavailable"})


class AdminLoginAdapterTests(unittest.TestCase):
    """admin_login(): the prompts, messages and master-override branch over
    verify_staff_login(), with the (ok, role, reauth) triple admin_panel() relies on."""

    def test_success_sets_the_global_and_returns_the_triple(self):
        prompts = iter(["bob", "pw"])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch("getpass.getpass", lambda _="": next(prompts)), \
             patch_main("get_connection",
                        return_value=_ScriptedConn([], [("FROM USERS", [_STAFF_OK])])), \
             patch_main("log_audit"), \
             patch_main("CURRENT_USER", None):
            result = app.admin_login()
            # Asserted inside the patch: mock restores the global on exit.
            self.assertEqual(app.CURRENT_USER, "bob")
        self.assertEqual(result, (True, "admin", False))

    def test_unknown_user_reprompts_until_a_valid_one(self):
        ok_row = _row(Password="pw", FailedAttempts=0, LockoutTime=None, Role="admin")
        prompts = iter(["nobody", "x", "bob", "pw"])

        def _users(params):
            return [ok_row] if params[0] == "bob" else []

        conn = _ScriptedConn([], [("FROM USERS", _users)])
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch("getpass.getpass", lambda _="": next(prompts)), \
             patch_main("get_connection", return_value=conn), \
             patch_main("log_audit"), \
             patch_main("CURRENT_USER", None):
            result = app.admin_login()
            self.assertEqual(app.CURRENT_USER, "bob")
        self.assertEqual(result, (True, "admin", False))

    def test_overridden_lockout_returns_reauth_true(self):
        prompts = iter(["bob", "wrong", "Y"])
        row = _row(Password="pw", FailedAttempts=2, LockoutTime=None, Role="manager")
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch("getpass.getpass", lambda _="": next(prompts)), \
             patch_main("get_connection", return_value=_ScriptedConn([], [("FROM USERS", [row])])), \
             patch_main("get_lockout_threshold", return_value=3), \
             patch_main("get_lockout_duration", return_value=30), \
             patch_main("require_master_override", return_value=True), \
             patch_main("clear_lockout", return_value=True) as unlock, \
             patch_main("log_audit"):
            result = app.admin_login()
        self.assertEqual(result, (False, "manager", True))  # reauth: creds required again
        unlock.assert_called_once_with("bob")

    def test_skipped_override_returns_with_no_login(self):
        prompts = iter(["bob", "wrong", "N"])
        row = _row(Password="pw", FailedAttempts=2, LockoutTime=None, Role="staff")
        with mock.patch("builtins.input", lambda _="": next(prompts)), \
             mock.patch("getpass.getpass", lambda _="": next(prompts)), \
             patch_main("get_connection", return_value=_ScriptedConn([], [("FROM USERS", [row])])), \
             patch_main("get_lockout_threshold", return_value=3), \
             patch_main("get_lockout_duration", return_value=30), \
             patch_main("require_master_override", return_value=True), \
             patch_main("clear_lockout", return_value=True), \
             patch_main("CURRENT_USER", None), \
             patch_main("log_audit"):
            result = app.admin_login()
            self.assertIsNone(app.CURRENT_USER)
        self.assertEqual(result, (False, None, False))


class CustomerLoginServiceTests(unittest.TestCase):
    """authenticate_customer(): the guest credential check the booking desk on both
    front-ends share."""

    ROW = _row(CustomerID=7, FirstName="Ada", LastName="Smith", Password="hunter2")

    def _conn(self, rows=None):
        return _ScriptedConn([], [("WHERE EMAIL = ?", list(rows or []))])

    def test_ok_verifies_and_audits_with_the_guest_as_actor(self):
        with patch_main("get_connection", return_value=self._conn([self.ROW])), \
             patch_main("log_audit") as audit:
            result = app.authenticate_customer("ada@example.com", "hunter2")
        self.assertEqual(result, {"status": "ok", "customer_id": 7, "first_name": "Ada"})
        audit.assert_called_once_with("LOGIN", "CustomerProfile", "ada@example.com",
                                      "Booking desk sign-in", user="ada@example.com")

    def test_wrong_password_is_not_a_login_and_does_not_audit(self):
        with patch_main("get_connection", return_value=self._conn([self.ROW])), \
             patch_main("log_audit") as audit:
            result = app.authenticate_customer("ada@example.com", "wrong")
        self.assertEqual(result, {"status": "wrong_password"})
        audit.assert_not_called()

    def test_discovery_call_reports_the_kind_without_comparing(self):
        # password=None is the console adapter's pre-prompt probe: is this email unknown,
        # passwordless, or ready?
        with patch_main("get_connection", return_value=self._conn([self.ROW])), \
             patch_main("log_audit") as audit:
            result = app.authenticate_customer("ada@example.com")
        self.assertEqual(result, {"status": "ready", "customer_id": 7, "first_name": "Ada"})
        audit.assert_not_called()

    def test_unknown_email_and_the_never_registered_markers(self):
        with patch_main("get_connection", return_value=self._conn([])), \
             patch_main("log_audit"):
            self.assertEqual(app.authenticate_customer("nobody@example.com", "x"),
                             {"status": "unknown"})
        no_password = _row(CustomerID=9, FirstName="Grace", LastName="Hopper", Password=None)
        with patch_main("get_connection", return_value=self._conn([no_password])), \
             patch_main("log_audit"):
            # Even with the right-looking input, an account without a password cannot sign in.
            self.assertEqual(app.authenticate_customer("grace@example.com", "x"),
                             {"status": "no_password"})

    def test_unavailable_when_migration_019_is_missing(self):
        with patch_main("get_connection",
                        side_effect=RuntimeError("Invalid column name 'Password'.")):
            result = app.authenticate_customer("ada@example.com", "hunter2")
        self.assertEqual(result, {"status": "unavailable"})


class CheckInServiceTests(unittest.TestCase):
    """check_in_eligibility() and perform_check_in(): the gate and the state changes."""

    WINDOW = _row(CheckInDate=date(2026, 3, 1), CheckOutDate=date(2026, 3, 4))
    PROFILE = _row(LastName="Smith", FirstName="Grace")
    LAST = _row(LastName="Smith")

    def _conn(self, window_rows=True):
        return _ScriptedConn([], [
            ("CHECKINDATE, CHECKOUTDATE", [self.WINDOW] if window_rows else []),
            ("LASTNAME, FIRSTNAME", [self.PROFILE]),
            ("SELECT LASTNAME FROM RESERVATIONS", [self.LAST]),
        ])

    def test_eligibility_ok_inside_the_window(self):
        with patch_main("get_connection", return_value=self._conn()), \
             patch_main("business_date", return_value=date(2026, 3, 2)):
            result = app.check_in_eligibility("9012")
        self.assertEqual(result, {"ok": True})

    def test_eligibility_refuses_outside_the_window(self):
        with patch_main("get_connection", return_value=self._conn()), \
             patch_main("business_date", return_value=date(2026, 3, 10)):
            result = app.check_in_eligibility("9012")
        self.assertEqual(result,
                         {"ok": False, "reason": "window", "check_in": date(2026, 3, 1),
                          "check_out": date(2026, 3, 4), "today": date(2026, 3, 10)})

    def test_eligibility_reports_a_missing_stay(self):
        with patch_main("get_connection", return_value=self._conn(window_rows=False)):
            result = app.check_in_eligibility("9012")
        self.assertEqual(result, {"ok": False, "reason": "no_stay"})

    def test_perform_check_in_runs_the_state_changes_and_audits_the_actor(self):
        with patch_main("get_connection", return_value=self._conn()), \
             patch_main("business_date", return_value=date(2026, 3, 2)), \
             patch_main("set_room_status") as status, \
             patch_main("upsert_customer_profile", return_value=7) as upsert, \
             patch_main("link_reservation_customer") as link, \
             patch_main("issue_key_card", return_value="KC-31337") as keycard, \
             patch_main("log_audit") as audit:
            result = app.perform_check_in("9012", "Grace", email="grace@example.com",
                                          phone="555-1234", actor="clerk")
        self.assertEqual(result, {"ok": True, "room_number": "9012",
                                  "key_card": "KC-31337", "customer_id": 7})
        status.assert_called_once_with("9012", "Occupied")
        upsert.assert_called_once_with("Smith", "Grace", "grace@example.com", "555-1234")
        link.assert_called_once_with("9012", 7)
        keycard.assert_called_once_with("9012", "Smith", "Grace")
        audit.assert_called_once_with("UPDATE", "Reservation", "9012",
                                      "Check-in completed for Grace (key card KC-31337)",
                                      user="clerk")

    def test_perform_check_in_refuses_outside_the_window_before_any_state_change(self):
        with patch_main("get_connection", return_value=self._conn()), \
             patch_main("business_date", return_value=date(2026, 3, 10)), \
             patch_main("set_room_status") as status, \
             patch_main("upsert_customer_profile") as upsert, \
             patch_main("issue_key_card") as keycard, \
             patch_main("log_audit") as audit:
            result = app.perform_check_in("9012", "Grace", actor="clerk")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["reason"], "window")
        status.assert_not_called()
        upsert.assert_not_called()
        keycard.assert_not_called()
        audit.assert_not_called()

    def test_perform_check_in_links_a_name_only_profile_when_contacts_are_blank(self):
        with patch_main("get_connection", return_value=self._conn()), \
             patch_main("business_date", return_value=date(2026, 3, 2)), \
             patch_main("set_room_status"), \
             patch_main("upsert_customer_profile", return_value=7) as upsert, \
             patch_main("link_reservation_customer"), \
             patch_main("issue_key_card"), \
             patch_main("log_audit"):
            result = app.perform_check_in("9012", "Grace", actor="clerk")
        self.assertEqual(result["customer_id"], 7)
        upsert.assert_called_once_with("Smith", "Grace", None, None)


class CreateBookingServiceTests(unittest.TestCase):
    """create_booking(): one transaction for claim + payment, with the card step injected."""

    def _call(self, txn, **overrides):
        defaults = dict(
            customer_id=7, last_name="Smith", first_name="Ada",
            room_type="Deluxe", check_in=date(2026, 3, 1), check_out=date(2026, 3, 3),
            pay_kind=app.PAYMENT_KIND_DEPOSIT, pay_amount=100.0,
            card_processor=lambda _amount: (True, "4242"), actor="manager")
        defaults.update(overrides)
        return app.create_booking(**defaults)

    def _patches(self, txn, overrides=None):
        """Enter every patch create_booking needs; returns (stack, mocks) with a mock
        handle per patched name. `overrides` swaps a patch value (name -> return value)."""
        values = {
            "get_connection": txn,
            "search_availability": [("9012", "Deluxe", "Available")],
            "_book_reservation_in_conn": (True, "reserved"),
            "new_booking_ref": "BK-4F2A9C",
            "business_date": date(2026, 3, 1),
            "stay_nights": 2,
            "upsert_room_if_missing": None,      # unbound: a fresh mock is fine
            "get_captured_nightly_rate": 200.0,
            "get_nightly_rate": 200.0,
            "stay_nightly_rate": 200.0,
            "log_audit": None,                   # unbound: a fresh mock is fine
            "_write_booking_charge": "BK-4F2A9C",
        }
        values.update(overrides or {})
        stack = ExitStack()
        mocks = {}
        for name, value in values.items():
            kwargs = {} if value is None else {"return_value": value}
            mocks[name] = stack.enter_context(patch_main(name, **kwargs))
        return stack, mocks

    def test_ok_claims_writes_the_charge_with_the_digits_and_commits(self):
        txn = _TxnConn()
        stack, mocks = self._patches(txn)
        with stack:
            result = self._call(txn)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["booking_ref"], "BK-4F2A9C")
        self.assertEqual(result["room_number"], "9012")
        self.assertEqual(result["nightly_rate"], 200.0)
        # The card_processor's digits flow into the charge write, not the process global.
        self.assertEqual(mocks["_write_booking_charge"].call_args.kwargs["card_last4"], "4242")
        # The deposit covers fewer nights than a prepayment.
        self.assertEqual(mocks["_write_booking_charge"].call_args.args[6],
                         min(app.BOOKING_DEPOSIT_NIGHTS, 2))
        self.assertEqual(txn.commits, 1)
        self.assertEqual(txn.rollbacks, 0)
        mocks["log_audit"].assert_called_once()
        self.assertEqual(mocks["log_audit"].call_args.kwargs["user"], "manager")
        mocks["upsert_room_if_missing"].assert_called_once_with("9012", room_type="Deluxe")

    def test_prepayment_covers_the_whole_stay(self):
        txn = _TxnConn()
        stack, mocks = self._patches(txn)
        with stack:
            result = self._call(txn, pay_kind=app.PAYMENT_KIND_PREPAYMENT, pay_amount=500.0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(mocks["_write_booking_charge"].call_args.args[6], 2)

    def test_a_sold_out_type_never_opens_a_transaction(self):
        txn = _TxnConn()
        stack, mocks = self._patches(txn, {"search_availability": []})
        with stack:
            result = self._call(txn)
        self.assertEqual(result["status"], "no_type")
        self.assertEqual(txn.commits, 0)
        mocks["_write_booking_charge"].assert_not_called()
        mocks["new_booking_ref"].assert_not_called()

    def test_no_claimable_candidate_rolls_back_without_charging(self):
        txn = _TxnConn()
        stack, mocks = self._patches(txn, {"_book_reservation_in_conn": (False, "room is Occupied")})
        with stack:
            result = self._call(txn)
        self.assertEqual(result["status"], "no_room")
        self.assertIn("room is Occupied", result["reasons"])
        self.assertEqual(txn.rollbacks, 1)
        self.assertEqual(txn.commits, 0)
        mocks["_write_booking_charge"].assert_not_called()

    def test_a_declined_card_rolls_back_the_room_claim(self):
        txn = _TxnConn()
        stack, mocks = self._patches(txn)
        with stack:
            result = self._call(txn, card_processor=lambda _amount: (False, None))
        self.assertEqual(result["status"], "declined")
        mocks["_book_reservation_in_conn"].assert_called_once()  # claimed, then returned
        self.assertEqual(txn.rollbacks, 1)
        self.assertEqual(txn.commits, 0)
        mocks["_write_booking_charge"].assert_not_called()

    def test_a_positive_amount_requires_a_card_processor(self):
        txn = _TxnConn()
        stack, _ = self._patches(txn)
        with stack:
            with self.assertRaises(ValueError):
                self._call(txn, card_processor=None)
        self.assertEqual(txn.commits, 0)

    def test_no_payment_kind_means_no_charge_row(self):
        txn = _TxnConn()
        stack, mocks = self._patches(txn)
        with stack:
            result = self._call(txn, pay_kind=None, pay_amount=0.0)
        self.assertEqual(result["status"], "ok")
        mocks["_write_booking_charge"].assert_not_called()
        self.assertEqual(txn.commits, 1)


class BookingPaymentDigitsTests(unittest.TestCase):
    """The card_last4 parameter frees a charge row from the process global."""

    def setUp(self):
        self._saved = session.LAST_CARD_DIGITS
        session.LAST_CARD_DIGITS = "9999"
        self.addCleanup(lambda: setattr(session, "LAST_CARD_DIGITS", self._saved))

    def test_an_explicit_last4_beats_the_global(self):
        log = []
        app.record_booking_payment("9012", "BK-AAAAAA", date(2026, 3, 1),
                                   app.PAYMENT_KIND_DEPOSIT, 100.0, conn=_PaymentConn(log),
                                   card_last4="4242")
        insert = [p for s, p in log if s.upper().startswith("INSERT INTO RESERVATIONPAYMENTS")]
        self.assertEqual(insert[0][6], "4242")

    def test_without_one_the_console_global_still_applies(self):
        log = []
        app.record_booking_payment("9012", "BK-AAAAAA", date(2026, 3, 1),
                                   app.PAYMENT_KIND_DEPOSIT, 100.0,
                                   conn=_PaymentConn(log))
        insert = [p for s, p in log if s.upper().startswith("INSERT INTO RESERVATIONPAYMENTS")]
        self.assertEqual(insert[0][6], "9999")

    def test_write_booking_charge_forwards_the_digits(self):
        calls = []

        def _fake(room_number, booking_ref, stay_check_in, kind, amount,
                  nights_covered=None, conn=None, card_last4=None):
            calls.append(card_last4)
            return 9

        with patch_main("record_booking_payment", side_effect=_fake):
            used = app._write_booking_charge(_TxnConn(), "9012", "BK-AAAAAA",
                                             date(2026, 3, 1), app.PAYMENT_KIND_DEPOSIT,
                                             100.0, 1, card_last4="4242")
        self.assertEqual(calls, ["4242"])
        # _write_booking_charge returns the reference, not the payment id.
        self.assertEqual(used, "BK-AAAAAA")


if __name__ == "__main__":
    unittest.main()