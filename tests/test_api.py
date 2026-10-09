# type: ignore
"""Phase-4 pilot endpoints (PLAN-web-api.md): login/logout/me, availability, booking and
check-in over the Starlette TestClient.

Every service the endpoints call is patched, so these exercise the HTTP layer -- the
status mapping, the signed cookie, the audit actor and the server-side quote -- without
touching a database. `payments.validate_card` is deliberately NOT patched: it is pure,
and the whole point of phase 3 is that the web path uses it instead of the process
global.
"""
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from unittest import mock

from fastapi.testclient import TestClient

import admin
import api
import booking_ledger
import bookings
import core
import customer
import loyalty
import reservations
import reports
import rooms


def _client():
    return TestClient(api.create_app())


def _login_staff(client, username="ada", role="manager"):
    with mock.patch.object(admin, "verify_staff_login",
                           return_value={"status": "ok", "role": role}):
        return client.post("/api/auth/login",
                           json={"kind": "staff", "username": username, "password": "pw"})


def _login_guest(client, email="g@example.com", customer_id=9, first_name="Gus"):
    with mock.patch.object(customer, "authenticate_customer",
                           return_value={"status": "ok", "customer_id": customer_id,
                                         "first_name": first_name}):
        return client.post("/api/auth/login",
                           json={"kind": "guest", "email": email, "password": "pw"})


def _booking_env(stack, create_result):
    """Patch the pricing inputs and the service a POST /api/bookings call reaches."""
    stack.enter_context(mock.patch.object(rooms, "get_room_types",
                                          return_value=[("Deluxe", 200.0)]))
    stack.enter_context(mock.patch.object(core, "business_date",
                                          return_value=date(2026, 10, 1)))
    stack.enter_context(mock.patch.object(core, "get_tax_rate", return_value=0.1))
    stack.enter_context(mock.patch.object(
        booking_ledger, "booking_payment_options",
        return_value=[(1, "dep", "Deposit", 200.0), (2, "full", "Prepayment", 220.0)]))
    return stack.enter_context(
        mock.patch.object(bookings, "create_booking", return_value=create_result))


BOOKING_BODY = {
    "last_name": "Guest", "first_name": "Gus", "room_type": "Deluxe",
    "check_in": "2026-11-01", "check_out": "2026-11-03", "pay_kind": "Prepayment",
    "card_number": "4111111111111111", "expiration_date": "12/2030", "cvv": "123",
}
BOOKING_OK = {"status": "ok", "booking_ref": "BR1", "room_number": "9012",
              "room_type": "Deluxe", "nightly_rate": 200.0, "card_last4": "1111"}


class HealthTests(unittest.TestCase):
    def test_health_probe(self):
        response = _client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class StaffLoginTests(unittest.TestCase):
    def test_ok_issues_a_cookie_and_me_shows_the_principal(self):
        client = _client()
        response = _login_staff(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {"kind": "staff", "username": "ada", "role": "manager"})
        self.assertEqual(client.get("/api/me").json()["username"], "ada")

    def test_not_found_is_401(self):
        with mock.patch.object(admin, "verify_staff_login", return_value={"status": "not_found"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "staff", "username": "x", "password": "pw"})
        self.assertEqual(response.status_code, 401)

    def test_bad_password_is_401_and_carries_the_attempt_count(self):
        with mock.patch.object(admin, "verify_staff_login",
                               return_value={"status": "bad_password", "attempts": 2, "role": "manager"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "staff", "username": "ada", "password": "bad"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["attempts"], 2)

    def test_locked_is_403(self):
        with mock.patch.object(admin, "verify_staff_login",
                               return_value={"status": "locked", "lockout_time": "2026-10-09 19:00"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "staff", "username": "ada", "password": "pw"})
        self.assertEqual(response.status_code, 403)

    def test_lockout_is_403_with_the_lockout_time(self):
        with mock.patch.object(admin, "verify_staff_login",
                               return_value={"status": "lockout", "attempts": 5, "role": "manager",
                                             "lockout_time": datetime(2026, 10, 9, 19, 0)}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "staff", "username": "ada", "password": "bad"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("lockout_time", response.json()["detail"])

    def test_unavailable_is_503(self):
        with mock.patch.object(admin, "verify_staff_login", return_value={"status": "unavailable"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "staff", "username": "ada", "password": "pw"})
        self.assertEqual(response.status_code, 503)


class GuestLoginTests(unittest.TestCase):
    def test_ok_issues_a_guest_cookie(self):
        client = _client()
        response = _login_guest(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["customer_id"], 9)
        me = client.get("/api/me").json()
        self.assertEqual(me, {"kind": "guest", "customer_id": 9, "email": "g@example.com"})

    def test_unknown_is_401(self):
        with mock.patch.object(customer, "authenticate_customer", return_value={"status": "unknown"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "guest", "email": "x@y.z", "password": "pw"})
        self.assertEqual(response.status_code, 401)

    def test_wrong_password_is_401(self):
        with mock.patch.object(customer, "authenticate_customer",
                               return_value={"status": "wrong_password"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "guest", "email": "g@x.y", "password": "bad"})
        self.assertEqual(response.status_code, 401)

    def test_no_password_is_403(self):
        with mock.patch.object(customer, "authenticate_customer",
                               return_value={"status": "no_password"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "guest", "email": "g@x.y", "password": "pw"})
        self.assertEqual(response.status_code, 403)

    def test_unavailable_is_503(self):
        with mock.patch.object(customer, "authenticate_customer",
                               return_value={"status": "unavailable"}):
            response = _client().post("/api/auth/login",
                                      json={"kind": "guest", "email": "g@x.y", "password": "pw"})
        self.assertEqual(response.status_code, 503)


class LogoutAndMeTests(unittest.TestCase):
    def test_me_without_a_cookie_is_401(self):
        self.assertEqual(_client().get("/api/me").status_code, 401)

    def test_logout_clears_the_cookie(self):
        client = _client()
        _login_staff(client)
        self.assertEqual(client.get("/api/me").status_code, 200)
        self.assertEqual(client.post("/api/auth/logout").json(), {"ok": True})
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_logout_when_signed_out_is_still_ok(self):
        self.assertEqual(_client().post("/api/auth/logout").status_code, 200)


class AvailabilityTests(unittest.TestCase):
    def test_lists_rooms_without_a_login(self):
        client = _client()
        with mock.patch.object(reservations, "search_availability",
                               return_value=[("9012", "Deluxe", "Available")]) as search:
            response = client.get("/api/rooms/availability",
                                  params={"check_in": "2026-11-01", "check_out": "2026-11-03"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {"rooms": [{"room_number": "9012", "room_type": "Deluxe",
                                     "status": "Available"}]})
        self.assertEqual(search.call_args.args, (date(2026, 11, 1), date(2026, 11, 3)))

    def test_backwards_window_is_400_without_querying(self):
        with mock.patch.object(reservations, "search_availability") as search:
            response = _client().get("/api/rooms/availability",
                                     params={"check_in": "2026-11-03", "check_out": "2026-11-01"})
        self.assertEqual(response.status_code, 400)
        search.assert_not_called()


class BookingTests(unittest.TestCase):
    def test_unauthenticated_is_401(self):
        with mock.patch.object(bookings, "create_booking") as create:
            response = _client().post("/api/bookings", json=BOOKING_BODY)
        self.assertEqual(response.status_code, 401)
        create.assert_not_called()

    def test_guest_booking_uses_their_own_id_and_email_as_the_actor(self):
        client = _client()
        _login_guest(client, email="g@example.com", customer_id=9)
        with ExitStack() as stack:
            create = _booking_env(stack, BOOKING_OK)
            response = client.post("/api/bookings", json=BOOKING_BODY)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["booking_ref"], "BR1")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["actor"], "g@example.com")
        self.assertEqual(kwargs["customer_id"], 9)
        self.assertEqual(kwargs["pay_amount"], 220.0)   # full stay, quoted server-side
        self.assertIsNotNone(kwargs["card_processor"])

    def test_a_client_cannot_underpay_a_deposit(self):
        client = _client()
        _login_guest(client)
        body = dict(BOOKING_BODY, pay_kind="Deposit")
        with ExitStack() as stack:
            create = _booking_env(stack, BOOKING_OK)
            client.post("/api/bookings", json=body)
        self.assertEqual(create.call_args.kwargs["pay_amount"], 200.0)

    def test_staff_may_book_for_a_customer(self):
        client = _client()
        _login_staff(client, username="ada")
        body = dict(BOOKING_BODY, customer_id=7)
        with ExitStack() as stack:
            create = _booking_env(stack, BOOKING_OK)
            client.post("/api/bookings", json=body)
        self.assertEqual(create.call_args.kwargs["actor"], "ada")
        self.assertEqual(create.call_args.kwargs["customer_id"], 7)

    def test_a_bad_card_is_400_and_never_reaches_the_service(self):
        client = _client()
        _login_guest(client)
        body = dict(BOOKING_BODY, card_number="1234")
        with ExitStack() as stack:
            create = _booking_env(stack, BOOKING_OK)
            response = client.post("/api/bookings", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid credit card number.")
        create.assert_not_called()

    def test_an_unknown_room_type_is_400(self):
        client = _client()
        _login_guest(client)
        with ExitStack() as stack:
            create = _booking_env(stack, BOOKING_OK)
            response = client.post("/api/bookings", json=dict(BOOKING_BODY, room_type="Nope"))
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_a_past_check_in_is_400(self):
        client = _client()
        _login_guest(client)
        with ExitStack() as stack:
            create = _booking_env(stack, BOOKING_OK)
            response = client.post("/api/bookings",
                                   json=dict(BOOKING_BODY, check_in="2026-09-01", check_out="2026-09-03"))
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_service_statuses_map_to_http(self):
        client = _client()
        _login_guest(client)
        cases = [
            ({"status": "declined"}, 402),
            ({"status": "unavailable"}, 503),
            ({"status": "no_type", "reasons": ["no Deluxe rooms"]}, 400),
            ({"status": "no_room", "reasons": ["room 9012: collision"]}, 409),
            ({"status": "payment_error"}, 500),
            ({"status": "commit_failed", "error": "boom"}, 500),
        ]
        for result, expected in cases:
            with self.subTest(status=result["status"]):
                with ExitStack() as stack:
                    _booking_env(stack, result)
                    self.assertEqual(client.post("/api/bookings", json=BOOKING_BODY).status_code,
                                     expected)


class CheckInTests(unittest.TestCase):
    def test_unauthenticated_is_401(self):
        self.assertEqual(_client().post("/api/reservations/9012/check-in",
                                        json={"first_name": "Gus"}).status_code, 401)

    def test_a_guest_session_is_403(self):
        client = _client()
        _login_guest(client)
        with mock.patch.object(reservations, "check_in_eligibility") as gate:
            response = client.post("/api/reservations/9012/check-in", json={"first_name": "Gus"})
        self.assertEqual(response.status_code, 403)
        gate.assert_not_called()

    def test_staff_check_in_names_the_actor(self):
        client = _client()
        _login_staff(client, username="ada")
        with mock.patch.object(reservations, "check_in_eligibility", return_value={"ok": True}), \
             mock.patch.object(reservations, "perform_check_in",
                               return_value={"ok": True, "room_number": "9012",
                                             "key_card": "K123", "customer_id": 5}) as perform:
            response = client.post("/api/reservations/9012/check-in",
                                   json={"first_name": "Gus", "email": "g@x.y", "phone": "555"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"room_number": "9012", "key_card": "K123",
                                           "customer_id": 5})
        self.assertEqual(perform.call_args.kwargs["actor"], "ada")

    def test_no_stay_is_404(self):
        client = _client()
        _login_staff(client)
        with mock.patch.object(reservations, "check_in_eligibility",
                               return_value={"ok": False, "reason": "no_stay"}), \
             mock.patch.object(reservations, "perform_check_in") as perform:
            response = client.post("/api/reservations/9999/check-in", json={"first_name": "Gus"})
        self.assertEqual(response.status_code, 404)
        perform.assert_not_called()

    def test_a_window_that_is_not_open_is_409(self):
        client = _client()
        _login_staff(client)
        gate = {"ok": False, "reason": "window", "check_in": "2026-12-01",
                "check_out": "2026-12-03", "today": "2026-11-20"}
        with mock.patch.object(reservations, "check_in_eligibility", return_value=gate):
            response = client.post("/api/reservations/9012/check-in", json={"first_name": "Gus"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["today"], "2026-11-20")

    def test_unavailable_is_503(self):
        client = _client()
        _login_staff(client)
        with mock.patch.object(reservations, "check_in_eligibility",
                               return_value={"ok": False, "reason": "unavailable"}):
            response = client.post("/api/reservations/9012/check-in", json={"first_name": "Gus"})
        self.assertEqual(response.status_code, 503)


class ReportTests(unittest.TestCase):
    def _staff(self):
        client = _client()
        _login_staff(client, username="ada")
        return client

    def test_rows_come_back_without_a_csv_file(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows",
                               return_value=(["PaymentID", "Amount"],
                                             [{"PaymentID": "1", "Amount": "50.00"}])):
            response = client.get("/api/reports/transactions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "report": "transactions",
            "headers": ["PaymentID", "Amount"],
            "rows": [{"PaymentID": "1", "Amount": "50.00"}],
        })

    def test_report_rows_dispatches_through_the_registry(self):
        # The registry binds function objects at build time, so patch it directly: the
        # dispatcher must forward the CLI-style params and drop the loyalty label.
        with mock.patch.dict(reports.REPORT_ROWS, {
            "loyalty": (lambda customer=None, room_number=None:
                        (["CustomerID"], [{"CustomerID": "5"}], customer or "x@y.z")),
        }):
            headers, rows = reports.report_rows("loyalty", customer="g@example.com")
        self.assertEqual(headers, ["CustomerID"])
        self.assertEqual(rows, [{"CustomerID": "5"}])

    def test_reports_require_a_staff_session(self):
        self.assertEqual(_client().get("/api/reports/transactions").status_code, 401)
        guest = _client()
        _login_guest(guest)
        self.assertEqual(guest.get("/api/reports/transactions").status_code, 403)

    def test_loyalty_takes_the_cli_room_option(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows") as rows:
            rows.return_value = (["CustomerID"], [{"CustomerID": "5"}])
            response = client.get("/api/reports/loyalty", params={"room": "9012"})
        self.assertEqual(response.status_code, 200)
        rows.assert_called_once_with("loyalty", room_number="9012")

    def test_revenue_passes_the_window(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows") as rows:
            rows.return_value = ([], [])
            response = client.get("/api/reports/revenue",
                                  params={"start": "2026-10-01", "end": "2026-10-05"})
        self.assertEqual(response.status_code, 200)
        rows.assert_called_once_with("revenue", start_date="2026-10-01", end_date="2026-10-05")

    def test_housekeeping_maps_the_date_option(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows") as rows:
            rows.return_value = ([], [])
            response = client.get("/api/reports/housekeeping", params={"floor": "9", "date": "2026-10-01"})
        self.assertEqual(response.status_code, 200)
        rows.assert_called_once_with("housekeeping", floor="9", on_date="2026-10-01")

    def test_booking_ledger_passes_the_reference(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows") as rows:
            rows.return_value = ([], [])
            response = client.get("/api/reports/booking_ledger", params={"booking_ref": "BR-1"})
        self.assertEqual(response.status_code, 200)
        rows.assert_called_once_with("booking_ledger", booking_ref="BR-1")

    def test_audit_passes_the_limit(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows") as rows:
            rows.return_value = ([], [])
            response = client.get("/api/reports/audit", params={"limit": 10})
        self.assertEqual(response.status_code, 200)
        rows.assert_called_once_with("audit", limit=10)

    def test_an_unknown_report_is_404_without_querying(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows") as rows:
            response = client.get("/api/reports/nope")
        self.assertEqual(response.status_code, 404)
        rows.assert_not_called()

    def test_a_bad_filter_reference_is_400(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows",
                               side_effect=ValueError("'guests' matches 2 customer profiles.")):
            response = client.get("/api/reports/loyalty", params={"customer": "guests"})
        self.assertEqual(response.status_code, 400)

    def test_an_unreachable_database_is_503(self):
        client = self._staff()
        with mock.patch.object(reports, "report_rows",
                               side_effect=RuntimeError("Database connection failed.")):
            response = client.get("/api/reports/transactions")
        self.assertEqual(response.status_code, 503)


class ReadEndpointsTests(unittest.TestCase):
    """Phase-5 read surfaces: the open room list, the staff board, and the guest's own
    bookings + loyalty. Every data call is patched, so no database is touched."""

    def _guest(self, email="g@example.com", customer_id=9):
        client = _client()
        _login_guest(client, email=email, customer_id=customer_id)
        return client

    def _staff(self):
        client = _client()
        _login_staff(client, username="ada")
        return client

    def test_room_types_are_open_and_mapped(self):
        with mock.patch.object(rooms, "get_room_types",
                               return_value=[("Deluxe", 200.0), ("Suite", 320.0)]):
            response = _client().get("/api/rooms")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": [
            {"room_type": "Deluxe", "nightly_rate": 200.0},
            {"room_type": "Suite", "nightly_rate": 320.0},
        ]})

    def test_board_is_staff_only(self):
        self.assertEqual(_client().get("/api/reservations/board").status_code, 401)
        guest = self._guest()
        self.assertEqual(guest.get("/api/reservations/board").status_code, 403)

    def test_board_defaults_to_the_business_date(self):
        client = self._staff()
        board = ([("9012", "Garcia", "Ana", date(2026, 10, 9), date(2026, 10, 12))], [], [])
        with mock.patch.object(core, "business_date", return_value=date(2026, 10, 9)), \
             mock.patch.object(reservations, "arrivals_departures_board",
                               return_value=board) as build:
            response = client.get("/api/reservations/board")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(build.call_args.args, (date(2026, 10, 9),))
        body = response.json()
        self.assertEqual(body["date"], "2026-10-09")
        self.assertEqual(body["arrivals"], [{
            "room_number": "9012", "last_name": "Garcia", "first_name": "Ana",
            "check_in": "2026-10-09", "check_out": "2026-10-12"}])
        self.assertEqual(body["in_house"], [])
        self.assertEqual(body["departures"], [])

    def test_board_accepts_an_explicit_date(self):
        client = self._staff()
        board = ([], [], [("9001", "Lee", "Bo", date(2026, 10, 9), date(2026, 10, 9))])
        with mock.patch.object(reservations, "arrivals_departures_board",
                               return_value=board) as build:
            response = client.get("/api/reservations/board", params={"on_date": "2026-10-09"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(build.call_args.args, (date(2026, 10, 9),))
        self.assertEqual(response.json()["departures"][0]["first_name"], "Bo")

    def test_guest_bookings_are_their_own(self):
        client = self._guest(customer_id=9)
        stays = [("9012", date(2026, 10, 1), date(2026, 10, 3)),
                 ("9011", date(2026, 9, 1), date(2026, 9, 2))]
        with mock.patch.object(bookings, "customer_stays", return_value=stays) as lookup:
            response = client.get("/api/guests/me/bookings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(lookup.call_args.args, (9,))
        body = response.json()
        self.assertEqual(body["customer_id"], 9)
        self.assertEqual(body["email"], "g@example.com")
        self.assertEqual(body["stays"], [
            {"room_number": "9012", "check_in": "2026-10-01", "check_out": "2026-10-03",
             "nights": 2},
            {"room_number": "9011", "check_in": "2026-09-01", "check_out": "2026-09-02",
             "nights": 1},
        ])

    def test_guest_bookings_require_a_guest_session(self):
        self.assertEqual(_client().get("/api/guests/me/bookings").status_code, 401)
        with mock.patch.object(bookings, "customer_stays") as lookup:
            response = self._staff().get("/api/guests/me/bookings")
        self.assertEqual(response.status_code, 403)
        lookup.assert_not_called()

    def test_loyalty_reads_balance_and_tier(self):
        client = self._guest(customer_id=9)
        details = {"tier": "Gold", "lifetime_points": 500, "points_multiplier": 1.5,
                   "discount_percent": 5.0, "perks": "Late checkout"}
        with mock.patch.object(loyalty, "get_points_by_customer", return_value=120) as points, \
             mock.patch.object(loyalty, "get_tier_details_by_customer",
                               return_value=details) as tier:
            response = client.get("/api/guests/me/loyalty")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(points.call_args.args, (9,))
        self.assertEqual(tier.call_args.args, (9,))
        self.assertEqual(response.json(), {
            "customer_id": 9, "email": "g@example.com", "eligible": True, "points": 120,
            "lifetime_points": 500, "tier": "Gold", "points_multiplier": 1.5,
            "discount_percent": 5.0, "perks": "Late checkout",
        })

    def test_loyalty_without_an_account_is_not_eligible(self):
        client = self._guest(customer_id=9)
        with mock.patch.object(loyalty, "get_points_by_customer", return_value=0), \
             mock.patch.object(loyalty, "get_tier_details_by_customer", return_value=None):
            response = client.get("/api/guests/me/loyalty")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["eligible"])
        self.assertIsNone(body["tier"])
        self.assertEqual(body["points"], 0)

    def test_loyalty_requires_a_guest_session(self):
        self.assertEqual(_client().get("/api/guests/me/loyalty").status_code, 401)
        self.assertEqual(self._staff().get("/api/guests/me/loyalty").status_code, 403)


class RequestValidationTests(unittest.TestCase):
    def test_login_without_a_password_is_422(self):
        response = _client().post("/api/auth/login", json={"kind": "staff", "username": "ada"})
        self.assertEqual(response.status_code, 422)

    def test_login_with_an_unknown_kind_is_422(self):
        response = _client().post("/api/auth/login", json={"kind": "elf", "username": "ada"})
        self.assertEqual(response.status_code, 422)

    def test_an_unknown_pay_kind_is_422(self):
        client = _client()
        _login_guest(client)
        response = client.post("/api/bookings", json=dict(BOOKING_BODY, pay_kind="Bitcoin"))
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
