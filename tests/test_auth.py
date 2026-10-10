# type: ignore
"""Phase-3 web auth: principal payloads, the signed-cookie round-trip, and per-request
resolution (PLAN-web-api.md). The module is deliberately FastAPI-free, so these run in
the repo's current environment; `_FakeRequest` stands in for the SessionMiddleware's
`request.session` dict.
"""
import unittest

from hotel import auth


class _FakeRequest:
    """Stand-in for a Starlette Request: only the `.session` dict the middleware exposes."""

    def __init__(self):
        self.session = {}


class PrincipalShapesTests(unittest.TestCase):
    def test_staff_principal_carries_username_and_role(self):
        self.assertEqual(
            auth.staff_principal("ada", "manager"),
            {"kind": "staff", "username": "ada", "role": "manager"},
        )

    def test_guest_principal_carries_customer_id_and_email(self):
        principal = auth.guest_principal(42, "guest@example.com")
        self.assertEqual(principal["kind"], "guest")
        self.assertEqual(principal["customer_id"], 42)
        self.assertEqual(principal["email"], "guest@example.com")

    def test_guest_principal_coerces_a_numeric_string_id(self):
        self.assertEqual(auth.guest_principal("7", "x@y.z")["customer_id"], 7)


class AuditActorTests(unittest.TestCase):
    def test_staff_audits_as_the_username(self):
        self.assertEqual(auth.audit_actor(auth.staff_principal("ada", "manager")), "ada")

    def test_guest_audits_as_the_email(self):
        self.assertEqual(
            auth.audit_actor(auth.guest_principal(1, "guest@example.com")),
            "guest@example.com",
        )

    def test_bad_principal_raises_rather_than_guessing(self):
        for bad in (None, {}, {"kind": "staff"}, {"kind": "elf", "username": "x"},
                    {"kind": "guest", "customer_id": 1}):
            with self.assertRaises(auth.NotAuthenticated):
                auth.audit_actor(bad)


class CookieRoundTripTests(unittest.TestCase):
    def test_issue_then_read_round_trips_through_the_cookie(self):
        request = _FakeRequest()
        auth.issue_principal(request, auth.staff_principal("ada", "manager"))
        self.assertIs(auth.current_principal(request), request.session["principal"])

    def test_clear_removes_the_principal(self):
        request = _FakeRequest()
        auth.issue_principal(request, auth.guest_principal(1, "g@x.y"))
        auth.clear_principal(request)
        with self.assertRaises(auth.NotAuthenticated):
            auth.current_principal(request)

    def test_a_second_issue_overwrites_the_first(self):
        request = _FakeRequest()
        auth.issue_principal(request, auth.staff_principal("old", "staff"))
        auth.issue_principal(request, auth.guest_principal(2, "new@x.y"))
        self.assertEqual(auth.audit_actor(auth.current_principal(request)), "new@x.y")


class CurrentPrincipalTests(unittest.TestCase):
    def test_empty_session_is_not_authenticated(self):
        with self.assertRaises(auth.NotAuthenticated):
            auth.current_principal(_FakeRequest())

    def test_malformed_payload_is_cleared_and_rejected(self):
        request = _FakeRequest()
        request.session["principal"] = {"kind": "staff", "username": "ada"}  # no role
        with self.assertRaises(auth.NotAuthenticated):
            auth.current_principal(request)
        self.assertNotIn(
            "principal", request.session,
            "a bad cookie must not keep half-authenticating after a failed read",
        )

    def test_guest_payload_requires_an_int_customer_id(self):
        # A JSON round-trip returns an int; a string id means the payload is not ours.
        request = _FakeRequest()
        request.session["principal"] = {"kind": "guest", "customer_id": "1", "email": "g@x.y"}
        with self.assertRaises(auth.NotAuthenticated):
            auth.current_principal(request)
        self.assertNotIn("principal", request.session)


if __name__ == "__main__":
    unittest.main()
