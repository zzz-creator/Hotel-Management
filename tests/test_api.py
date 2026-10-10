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
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import api
from hotel import (
    admin, billing, booking_ledger, bookings, core, customer, items, loyalty, orders,
    reservations, reports, rooms,
)


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


class InvoiceReadTests(unittest.TestCase):
    """Phase-5 billing reads: invoice list/detail, the live folio and the settlement
    residue. All staff-only (billing names a guest's money), every data call patched."""

    def _staff(self):
        client = _client()
        _login_staff(client, username="ada")
        return client

    def test_room_invoices_are_staff_only(self):
        self.assertEqual(_client().get("/api/rooms/9012/invoices").status_code, 401)
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(billing, "list_invoices_for_room") as rows:
            self.assertEqual(guest.get("/api/rooms/9012/invoices").status_code, 403)
        rows.assert_not_called()

    def test_room_invoices_list(self):
        rows = [(101, "9012", date(2026, 10, 1), 276.85, 276.85),
                (100, "9012", date(2026, 9, 1), 220.00, 0.0)]
        with mock.patch.object(billing, "list_invoices_for_room", return_value=rows) as find:
            response = self._staff().get("/api/rooms/9012/invoices")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(find.call_args.args, ("9012",))
        self.assertEqual(response.json(), {
            "room_number": "9012",
            "invoices": [
                {"invoice_id": 101, "room_number": "9012", "invoice_date": "2026-10-01",
                 "total_amount": 276.85, "amount_paid": 276.85},
                {"invoice_id": 100, "room_number": "9012", "invoice_date": "2026-09-01",
                 "total_amount": 220.0, "amount_paid": 0.0},
            ],
        })

    def test_invoice_detail_is_staff_only(self):
        self.assertEqual(_client().get("/api/invoices/101").status_code, 401)
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(billing, "load_invoice") as load:
            self.assertEqual(guest.get("/api/invoices/101").status_code, 403)
        load.assert_not_called()

    def test_invoice_detail_itemized(self):
        header = SimpleNamespace(
            InvoiceID=101, RoomNumber="9012", InvoiceDate=date(2026, 10, 1),
            GuestName=" Ana Garcia ", Subtotal=250.0, DiscountCodeAmount=0.0,
            TierDiscountAmount=0.0, TaxAmount=25.0, TotalAmount=275.0,
            PointsRedeemed=0, RedemptionValue=0.0, AmountPaid=275.0,
            PrepaidAmount=0.0, RoomSubtotal=200.0, RoomTaxAmount=20.0, RoomTotal=220.0,
            FnbSubtotal=50.0, FnbDiscountCodeAmount=0.0, FnbTierDiscountAmount=0.0,
            FnbTaxAmount=5.0)
        items = [
            SimpleNamespace(ID=1, ItemName="Room charge for 2026-10-01 - Deluxe",
                            Quantity=2, UnitPrice=100.0, Amount=200.0, PaidEarlier=0,
                            ChargeGroup="Room", CreatedAt=datetime(2026, 10, 1, 12, 0)),
            SimpleNamespace(ID=2, ItemName="Club Sandwich", Quantity=2, UnitPrice=12.5,
                            Amount=25.0, PaidEarlier=1, ChargeGroup="F&B",
                            CreatedAt=datetime(2026, 10, 1, 13, 0)),
        ]
        payments = [
            SimpleNamespace(Kind="Prepayment", Amount=200.0, CardLast4="1111",
                            PaidAt=datetime(2026, 10, 1, 10, 30), Notes=None,
                            AppliedToInvoiceID=101),
        ]
        with mock.patch.object(billing, "load_invoice",
                               return_value=(header, items, payments)) as load:
            response = self._staff().get("/api/invoices/101")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(load.call_args.args, (101,))
        body = response.json()
        self.assertEqual(body["invoice_id"], 101)
        self.assertEqual(body["guest_name"], "Ana Garcia")
        self.assertEqual(body["total_amount"], 275.0)
        self.assertEqual(body["room_total"], 220.0)
        self.assertEqual(body["fnb_subtotal"], 50.0)
        self.assertEqual(body["items"], [
            {"id": 1, "item_name": "Room charge for 2026-10-01 - Deluxe", "quantity": 2,
             "unit_price": 100.0, "amount": 200.0, "paid_earlier": False,
             "charge_group": "Room", "created_at": "2026-10-01 12:00:00"},
            {"id": 2, "item_name": "Club Sandwich", "quantity": 2, "unit_price": 12.5,
             "amount": 25.0, "paid_earlier": True, "charge_group": "F&B",
             "created_at": "2026-10-01 13:00:00"},
        ])
        self.assertEqual(body["payments"], [
            {"kind": "Prepayment", "amount": 200.0, "card_last4": "1111",
             "paid_at": "2026-10-01 10:30:00", "notes": None,
             "applied_to_invoice_id": 101},
        ])

    def test_invoice_not_found_is_404(self):
        with mock.patch.object(billing, "load_invoice", return_value=None):
            response = self._staff().get("/api/invoices/999")
        self.assertEqual(response.status_code, 404)

    def test_invoice_unreachable_is_503(self):
        with mock.patch.object(billing, "load_invoice",
                               side_effect=RuntimeError("Database connection failed.")):
            response = self._staff().get("/api/invoices/101")
        self.assertEqual(response.status_code, 503)

    def test_folio_lists_unbilled_lines(self):
        rows = [
            (1, "Room charge for 2026-10-01 - Deluxe", 2, 100.0, 200.0, 0, "Room",
             datetime(2026, 10, 1, 12, 0)),
            (2, "Club Sandwich", 2, 12.5, 25.0, 1, "F&B", datetime(2026, 10, 1, 13, 0)),
        ]
        with mock.patch.object(billing, "open_folio", return_value=rows) as folio:
            response = self._staff().get("/api/rooms/9012/folio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(folio.call_args.args, ("9012",))
        body = response.json()
        self.assertEqual(body["room_number"], "9012")
        self.assertEqual(body["lines"], [
            {"id": 1, "item_name": "Room charge for 2026-10-01 - Deluxe", "quantity": 2,
             "unit_price": 100.0, "amount": 200.0, "paid_earlier": False,
             "charge_group": "Room", "created_at": "2026-10-01 12:00:00"},
            {"id": 2, "item_name": "Club Sandwich", "quantity": 2, "unit_price": 12.5,
             "amount": 25.0, "paid_earlier": True, "charge_group": "F&B",
             "created_at": "2026-10-01 13:00:00"},
        ])

    def test_folio_is_staff_only(self):
        self.assertEqual(_client().get("/api/rooms/9012/folio").status_code, 401)
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(billing, "open_folio") as folio:
            self.assertEqual(guest.get("/api/rooms/9012/folio").status_code, 403)
        folio.assert_not_called()

    def test_outstanding_reports_the_residue(self):
        residue = ["no invoice was ever issued, so the bill was never settled",
                   "2 charge(s) are still unbilled"]
        with mock.patch.object(billing, "settlement_outstanding", return_value=residue) as scan:
            response = self._staff().get("/api/rooms/9012/outstanding")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(scan.call_args.args, ("9012",))
        self.assertEqual(response.json(),
                         {"room_number": "9012", "outstanding": residue})

    def test_outstanding_is_staff_only(self):
        self.assertEqual(_client().get("/api/rooms/9012/outstanding").status_code, 401)
        guest = _client()
        _login_guest(guest)
        self.assertEqual(guest.get("/api/rooms/9012/outstanding").status_code, 403)

    def test_print_invoice_is_a_renderer_over_load_invoice(self):
        # The console renderer must not grow a second copy of the invoice query: it
        # delegates all data to the twin the API reads.
        with mock.patch.object(billing, "load_invoice", return_value=None) as load:
            self.assertFalse(billing.print_invoice(5))
        self.assertEqual(load.call_args.args, (5,))
        with mock.patch.object(billing, "load_invoice",
                               side_effect=RuntimeError("Database connection failed.")):
            self.assertFalse(billing.print_invoice(5))


class OrderReadTests(unittest.TestCase):
    """Phase-5 order reads: room list, single order detail, the guest's own orders."""

    def _staff(self):
        client = _client()
        _login_staff(client, username="ada")
        return client

    def test_room_orders_are_staff_only(self):
        self.assertEqual(_client().get("/api/rooms/9012/orders").status_code, 401)
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(orders, "get_orders_for_room") as rows:
            self.assertEqual(guest.get("/api/rooms/9012/orders").status_code, 403)
        rows.assert_not_called()

    def test_room_orders_list(self):
        rows = [(7, "Placed", datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 1, 9, 0),
                 "2x Club Sandwich", None)]
        with mock.patch.object(orders, "get_orders_for_room", return_value=rows) as find:
            response = self._staff().get("/api/rooms/9012/orders")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(find.call_args.args, ("9012",))
        self.assertEqual(find.call_args.kwargs, {"active_only": False})
        self.assertEqual(response.json(), {"room_number": "9012", "orders": [
            {"order_id": 7, "status": "Placed", "placed_at": "2026-10-01 09:00:00",
             "updated_at": "2026-10-01 09:00:00", "items": "2x Club Sandwich",
             "notes": None},
        ]})

    def test_room_orders_can_filter_active_only(self):
        with mock.patch.object(orders, "get_orders_for_room", return_value=[]) as find:
            self._staff().get("/api/rooms/9012/orders?active_only=true")
        self.assertEqual(find.call_args.kwargs, {"active_only": True})

    def test_order_detail_is_staff_only(self):
        self.assertEqual(_client().get("/api/orders/7").status_code, 401)
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(orders, "get_order") as get:
            self.assertEqual(guest.get("/api/orders/7").status_code, 403)
        get.assert_not_called()

    def test_order_detail(self):
        record = (7, "9012", "Preparing", datetime(2026, 10, 1, 9, 0),
                  datetime(2026, 10, 1, 9, 30), None)
        with mock.patch.object(orders, "get_order", return_value=record) as get, \
                mock.patch.object(orders, "get_order_items",
                                  return_value=[("Club Sandwich", 2, 12.5)]):
            response = self._staff().get("/api/orders/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_args.args, (7,))
        self.assertEqual(response.json(), {
            "order_id": 7, "room_number": "9012", "status": "Preparing",
            "placed_at": "2026-10-01 09:00:00", "updated_at": "2026-10-01 09:30:00",
            "notes": None, "items": [{"item_name": "Club Sandwich", "quantity": 2,
                                      "unit_price": 12.5}],
        })

    def test_order_detail_missing_is_404(self):
        with mock.patch.object(orders, "get_order", return_value=None):
            self.assertEqual(self._staff().get("/api/orders/7").status_code, 404)

    def test_order_detail_unreachable_is_503(self):
        with mock.patch.object(orders, "get_order",
                               side_effect=RuntimeError("Database connection failed.")):
            self.assertEqual(self._staff().get("/api/orders/7").status_code, 503)

    def test_my_orders_is_guest_only(self):
        self.assertEqual(_client().get("/api/guests/me/orders").status_code, 401)
        staff = _client()
        _login_staff(staff)
        self.assertEqual(staff.get("/api/guests/me/orders").status_code, 403)

    def test_my_orders_aggregates_only_own_rooms(self):
        guest = _client()
        _login_guest(guest)
        stays = [("9012", date(2026, 10, 1), date(2026, 10, 3)),
                 ("9011", date(2026, 9, 1), date(2026, 9, 3))]
        with mock.patch.object(bookings, "customer_stays", return_value=stays), \
                mock.patch.object(orders, "get_orders_for_room",
                                  side_effect=lambda room, active_only=False: {
                                      "9012": [(7, "Delivered", datetime(2026, 10, 1, 9, 0),
                                                datetime(2026, 10, 1, 10, 0), "Pizza", None)],
                                      "9011": [(3, "Placed", datetime(2026, 9, 1, 8, 0),
                                                datetime(2026, 9, 1, 8, 0), "Tea", None)],
                                  }[room]):
            response = guest.get("/api/guests/me/orders")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([o["order_id"] for o in response.json()["orders"]], [7, 3])
        self.assertNotIn("9013", response.json())


class OrderWriteTests(unittest.TestCase):
    """Phase-5 order writes: placement (bill or pay-now) and the staff lifecycle move."""

    def _ok_result(self, **overrides):
        result = {"status": "ok", "order_id": 7, "total": 25.0, "paid": 0.0,
                  "final_total": 25.0, "discounted_subtotal": 25.0, "tax": 0.0,
                  "savings_lines": []}
        result.update(overrides)
        return result

    def test_create_order_is_signed_in_only(self):
        self.assertEqual(
            _client().post("/api/rooms/9012/orders",
                           json={"items": [{"item_id": 1, "quantity": 2}]}).status_code, 401)

    def test_staff_bills_an_order(self):
        staff = _client()
        _login_staff(staff, username="ada")
        with mock.patch.object(items, "get_dynamic_price", return_value=12.5), \
                mock.patch.object(orders, "place_order", return_value=self._ok_result()) as place:
            response = staff.post("/api/rooms/9012/orders",
                                  json={"items": [{"item_id": 1, "quantity": 2}],
                                        "pay_mode": "bill"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(place.call_args.args, ("9012", [(1, 12.5, 2)]))
        self.assertEqual(place.call_args.kwargs["pay_mode"], "bill")
        self.assertIsNone(place.call_args.kwargs["discount_code"])
        self.assertEqual(place.call_args.kwargs["actor"], "ada")
        self.assertEqual(response.json()["order_id"], 7)
        self.assertEqual(response.json()["paid"], 0.0)

    def test_pay_now_requires_a_card(self):
        staff = _client()
        _login_staff(staff)
        with mock.patch.object(orders, "place_order") as place:
            response = staff.post("/api/rooms/9012/orders",
                                  json={"items": [{"item_id": 1, "quantity": 2}],
                                        "pay_mode": "pay_now"})
        self.assertEqual(response.status_code, 400)
        place.assert_not_called()

    def test_pay_now_charges_and_prices_server_side(self):
        staff = _client()
        _login_staff(staff, username="ada")
        with mock.patch.object(items, "get_dynamic_price", return_value=12.5), \
                mock.patch.object(orders, "place_order",
                                  return_value=self._ok_result(paid=23.5,
                                                               final_total=23.5)) as place:
            response = staff.post("/api/rooms/9012/orders",
                                  json={"items": [{"item_id": 1, "quantity": 2}],
                                        "pay_mode": "pay_now",
                                        "discount_code": "SAVE10",
                                        "card_number": "4111111111111111",
                                        "expiration_date": "12/2030", "cvv": "123"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(place.call_args.kwargs["discount_code"], "SAVE10")
        self.assertEqual(place.call_args.kwargs["actor"], "ada")
        self.assertEqual(response.json()["paid"], 23.5)

    def test_guest_orders_only_their_own_room(self):
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(bookings, "customer_stays",
                               return_value=[("9012", date(2026, 10, 1), date(2026, 10, 3))]), \
                mock.patch.object(orders, "place_order", return_value=self._ok_result()) as place:
            foreign = guest.post("/api/rooms/9001/orders",
                                 json={"items": [{"item_id": 1, "quantity": 1}]})
            self.assertEqual(foreign.status_code, 403)
            own = guest.post("/api/rooms/9012/orders",
                             json={"items": [{"item_id": 1, "quantity": 1}]})
            self.assertEqual(own.status_code, 201)
        self.assertEqual(place.call_count, 1)

    def test_declined_card_is_402(self):
        staff = _client()
        _login_staff(staff)
        with mock.patch.object(items, "get_dynamic_price", return_value=12.5), \
                mock.patch.object(orders, "place_order",
                                  return_value={"status": "declined", "order_id": None,
                                                "total": 25.0, "paid": 0.0}):
            response = staff.post("/api/rooms/9012/orders",
                                  json={"items": [{"item_id": 1, "quantity": 2}],
                                        "pay_mode": "pay_now",
                                        "card_number": "4111111111111111",
                                        "expiration_date": "12/2030", "cvv": "123"})
        self.assertEqual(response.status_code, 402)

    def test_advance_is_staff_only(self):
        self.assertEqual(_client().post("/api/orders/7/advance",
                                        json={"action": "advance"}).status_code, 401)
        guest = _client()
        _login_guest(guest)
        with mock.patch.object(orders, "advance_order") as move:
            self.assertEqual(guest.post("/api/orders/7/advance",
                                        json={"action": "advance"}).status_code, 403)
        move.assert_not_called()

    def test_advance_moves_and_names_the_actor(self):
        record = (7, "9012", "Placed", datetime(2026, 10, 1, 9, 0),
                  datetime(2026, 10, 1, 9, 0), None)
        staff = _client()
        _login_staff(staff, username="ada")
        with mock.patch.object(orders, "get_order", return_value=record), \
                mock.patch.object(orders, "advance_order", return_value="Preparing") as move:
            response = staff.post("/api/orders/7/advance", json={"action": "advance"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"order_id": 7, "status": "Preparing"})
        self.assertEqual(move.call_args.args, (7,))
        self.assertEqual(move.call_args.kwargs["action"], "advance")
        self.assertEqual(move.call_args.kwargs["actor"], "ada")

    def test_advance_missing_order_is_404(self):
        staff = _client()
        _login_staff(staff)
        with mock.patch.object(orders, "get_order", return_value=None):
            response = staff.post("/api/orders/7/advance", json={"action": "cancel"})
        self.assertEqual(response.status_code, 404)

    def test_advance_unreachable_is_503(self):
        staff = _client()
        _login_staff(staff)
        with mock.patch.object(orders, "get_order",
                               side_effect=RuntimeError("Database connection failed.")):
            response = staff.post("/api/orders/7/advance", json={"action": "advance"})
        self.assertEqual(response.status_code, 503)

    def test_advance_that_fails_is_500(self):
        record = (7, "9012", "Placed", datetime(2026, 10, 1, 9, 0),
                  datetime(2026, 10, 1, 9, 0), None)
        staff = _client()
        _login_staff(staff)
        with mock.patch.object(orders, "get_order", return_value=record), \
                mock.patch.object(orders, "advance_order", return_value=None):
            response = staff.post("/api/orders/7/advance", json={"action": "advance"})
        self.assertEqual(response.status_code, 500)


class AdminReadTests(unittest.TestCase):
    """Phase-5 admin reads: users/items/discounts (manager+) and promotions (admin)."""

    def _login(self, role):
        client = _client()
        _login_staff(client, username="ada", role=role)
        return client

    def test_admin_reads_require_a_staff_session(self):
        self.assertEqual(_client().get("/api/admin/users").status_code, 401)
        guest = _client()
        _login_guest(guest)
        self.assertEqual(guest.get("/api/admin/users").status_code, 403)
        self.assertEqual(guest.get("/api/admin/promotions").status_code, 403)

    def test_a_plain_staff_account_is_blocked_from_all(self):
        staff = self._login("staff")
        self.assertEqual(staff.get("/api/admin/users").status_code, 403)
        self.assertEqual(staff.get("/api/admin/items").status_code, 403)
        self.assertEqual(staff.get("/api/admin/discounts").status_code, 403)
        self.assertEqual(staff.get("/api/admin/promotions").status_code, 403)

    def test_users_list_for_manager_and_admin(self):
        for role in ("manager", "admin"):
            client = self._login(role)
            with mock.patch.object(admin, "list_users",
                                   return_value=[("ada", role)]):
                response = client.get("/api/admin/users")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(),
                             {"users": [{"username": "ada", "role": role}]})

    def test_items_list_for_manager_and_admin(self):
        with mock.patch.object(items, "list_items",
                               return_value=[(1, "Club Sandwich", 12.5, "F&B")]) as find:
            response = self._login("manager").get("/api/admin/items")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [
            {"item_id": 1, "name": "Club Sandwich", "price": 12.5, "pricing_rule": "F&B"}]})

    def test_discounts_list_for_manager_and_admin(self):
        with mock.patch.object(admin, "list_discount_codes",
                               return_value=[("SAVE10", 10)]):
            response = self._login("manager").get("/api/admin/discounts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {"discounts": [{"code": "SAVE10", "percentage": 10.0}]})

    def test_promotions_are_admin_only(self):
        manager = self._login("manager")
        self.assertEqual(manager.get("/api/admin/promotions").status_code, 403)
        with mock.patch.object(orders, "get_promotions", return_value=[
                (3, "Spring", "10% off", "SPRING")]):
            response = self._login("admin").get("/api/admin/promotions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"promotions": [
            {"promotion_id": 3, "title": "Spring", "details": "10% off",
             "discount_code": "SPRING"}]})


class AdminWriteTests(unittest.TestCase):
    """Phase-5 admin writes: create user / unlock / reset password, all admin-only."""

    def _login(self, role):
        client = _client()
        _login_staff(client, username="ada", role=role)
        return client

    def test_writes_are_admin_only(self):
        anonymous = _client()
        self.assertEqual(
            anonymous.post("/api/admin/users", json={
                "username": "bob", "password": "pw", "role": "staff"}).status_code,
            401)
        for role in ("staff", "manager"):
            client = self._login(role)
            self.assertEqual(
                client.post("/api/admin/users", json={
                    "username": "bob", "password": "pw", "role": "staff"}).status_code,
                403)
            self.assertEqual(
                client.post("/api/admin/users/bob/unlock").status_code, 403)
            self.assertEqual(
                client.post("/api/admin/users/bob/password",
                            json={"password": "pw"}).status_code, 403)

    def test_create_user_returns_201_and_names_the_actor(self):
        client = self._login("admin")
        with mock.patch.object(admin, "create_user", return_value="created") as find:
            response = client.post("/api/admin/users", json={
                "username": "bob", "password": "pw", "role": "staff"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(),
                         {"username": "bob", "role": "staff"})
        self.assertEqual(find.call_args.args, ("bob", "pw", "staff"))
        self.assertEqual(find.call_args.kwargs["actor"], "ada")

    def test_create_user_maps_each_outcome(self):
        client = self._login("admin")
        for outcome, status in (("exists", 409), ("invalid_role", 400),
                                ("error", 500)):
            with mock.patch.object(admin, "create_user", return_value=outcome):
                response = client.post("/api/admin/users", json={
                    "username": "bob", "password": "pw", "role": "staff"})
            self.assertEqual(response.status_code, status, outcome)

    def test_create_user_rejects_a_role_the_console_cannot_use(self):
        # The real create_user, unpatched: the role gate returns before any DB open,
        # so the endpoint answers 400 without a database.
        client = self._login("admin")
        response = client.post("/api/admin/users", json={
            "username": "bob", "password": "pw", "role": "wizard"})
        self.assertEqual(response.status_code, 400)

    def test_unlock_maps_true_and_false(self):
        client = self._login("admin")
        with mock.patch.object(admin, "clear_lockout", return_value=True) as find:
            response = client.post("/api/admin/users/bob/unlock")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"username": "bob", "status": "unlocked"})
        self.assertEqual(find.call_args.args, ("bob",))
        with mock.patch.object(admin, "clear_lockout", return_value=False):
            self.assertEqual(
                client.post("/api/admin/users/bob/unlock").status_code, 500)

    def test_reset_password_maps_each_outcome_and_names_the_actor(self):
        client = self._login("admin")
        with mock.patch.object(admin, "set_password", return_value="ok") as find:
            response = client.post("/api/admin/users/bob/password",
                                   json={"password": "pw"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"username": "bob", "status": "reset"})
        self.assertEqual(find.call_args.args, ("bob", "pw"))
        self.assertEqual(find.call_args.kwargs["actor"], "ada")
        for outcome, status in (("not_found", 404), ("error", 500)):
            with mock.patch.object(admin, "set_password", return_value=outcome):
                response = client.post("/api/admin/users/bob/password",
                                       json={"password": "pw"})
            self.assertEqual(response.status_code, status, outcome)


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
