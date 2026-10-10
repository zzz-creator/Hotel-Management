import os
import unittest
from unittest import mock

from hotel import clearance


class TestCardExtraction(unittest.TestCase):
    def _svgs(self):
        return sorted(f for f in os.listdir(clearance.ASSETS_DIR)
                      if f.endswith(".svg"))

    def test_every_svg_yields_a_name(self):
        for filename in self._svgs():
            key = filename[:-4]
            card = clearance.get_card(key)
            self.assertTrue(card["name"], f"{key} has no extractable name")
            self.assertTrue(card["badge"], f"{key} has no extractable badge")

    def test_omega_name(self):
        self.assertEqual(
            clearance.extract_card_name(clearance.card_path("Omega")),
            "DIRECTORY OVERSEER")

    def test_visitor_badge(self):
        self.assertEqual(
            clearance.extract_badge(clearance.card_path("Visitor")),
            "VISITOR PASS")

    def test_matrix_covers_all_categories_and_tiers(self):
        for tier, row in clearance.GUEST_MATRIX.items():
            self.assertEqual(len(row), len(clearance.CATEGORIES), tier)

    def test_platinum_deluxe_and_up_is_chi(self):
        for category in clearance.CATEGORIES[1:]:
            self.assertEqual(
                clearance.guest_card_key("Platinum", category), "Chi", category)

    def test_platinum_standard_is_phi(self):
        self.assertEqual(clearance.guest_card_key("Platinum", "Standard"), "Phi")

    def test_every_matrix_card_exists_on_disk(self):
        keys = {k for row in clearance.GUEST_MATRIX.values() for k in row}
        keys |= set(clearance.ROLE_CARDS.values()) | {clearance.VISITOR, clearance.MAYDAY}
        for key in keys:
            self.assertTrue(os.path.exists(clearance.card_path(key)), key)

    def test_every_svg_is_reachable(self):
        used = {k for row in clearance.GUEST_MATRIX.values() for k in row}
        used |= set(clearance.ROLE_CARDS.values()) | {clearance.VISITOR, clearance.MAYDAY}
        on_disk = {f[:-4] for f in os.listdir(clearance.ASSETS_DIR)
                   if f.endswith(".svg")}
        self.assertEqual(used, on_disk)

    def test_unknown_tier_falls_back_to_bronze_row(self):
        self.assertEqual(clearance.guest_card_key("Diamond", "Standard"), "Alpha")

    def test_no_account_is_visitor(self):
        self.assertEqual(
            clearance.clearance_for_guest(None, None, has_loyalty_account=False),
            "Visitor")

    def test_emergency_is_mayday(self):
        self.assertEqual(
            clearance.clearance_for_guest("Gold", "Suite", emergency=True),
            "Mayday")

    def test_roles(self):
        self.assertEqual(clearance.clearance_for_role("admin"), "Omega")
        self.assertEqual(clearance.clearance_for_role("manager"), "Psi")
        self.assertEqual(clearance.clearance_for_role("staff"), "Sigma")
        self.assertEqual(clearance.clearance_for_role("nobody"), "Visitor")


class TestGuestSubject(unittest.TestCase):
    """The desk names the customer and their loyalty status -- never the room."""

    def test_customer_and_loyalty(self):
        self.assertEqual(
            clearance.guest_subject({"name": "Ada Doe", "tier": "Gold"}, "10203"),
            "Customer: Ada Doe    Loyalty: Gold tier")

    def test_customer_without_an_account(self):
        self.assertEqual(
            clearance.guest_subject({"name": "Ada Doe", "tier": None}, "10203"),
            "Customer: Ada Doe    Loyalty: no loyalty account")

    def test_vacant_room_names_no_one(self):
        self.assertEqual(
            clearance.guest_subject({"name": None, "tier": None}, "10203"),
            "No guest checked in")

    def test_the_room_number_is_not_echoed(self):
        for guest in ({"name": "Ada Doe", "tier": "Gold"},
                      {"name": None, "tier": None}):
            self.assertNotIn("10203", clearance.guest_subject(guest, "10203"))


class _FakeCursor:
    """Answers the three lookups clearance.lookup() makes, off its connection."""

    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self._index = 0

    def execute(self, sql, params=()):
        self.conn.queries.append(" ".join(sql.split()))
        upper = sql.upper()
        if "FROM USERS" in upper:
            self._rows = []
        elif "FROM ROOMS" in upper:
            self._rows = [("Standard",)]
        elif "FROM RESERVATIONS" in upper:
            self._rows = [("Doe", "Ada", self.conn.customer_id)]
        elif "FROM LOYALTYACCOUNTS" in upper:
            self._rows = [(self.conn.tier,)]
        else:
            self._rows = []
        self._index = 0
        return self

    def fetchone(self):
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None


class _FakeConn:
    def __init__(self, customer_id=7, tier="Gold"):
        self.customer_id = customer_id
        self.tier = tier
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self)


class TestLookupSubject(unittest.TestCase):
    def _lookup(self, **kwargs):
        conn = _FakeConn(**kwargs)
        with mock.patch.object(clearance, "get_connection", return_value=conn):
            result = clearance.lookup("10203")
        return result, conn

    def test_room_lookup_shows_the_customer_not_the_room(self):
        (card, subject), _conn = self._lookup()
        self.assertEqual(card, clearance.guest_card_key("Gold", "Standard"))
        self.assertEqual(subject, "Customer: Ada Doe    Loyalty: Gold tier")

    def test_linked_stay_reads_the_tier_by_customer(self):
        # The name and the tier must belong to the same person, so a linked stay
        # reads LoyaltyAccounts by the stay's CustomerID, not by room pointer.
        _result, conn = self._lookup(customer_id=7)
        loyalty_sql = next(q for q in conn.queries if "FROM LOYALTYACCOUNTS" in q.upper())
        self.assertIn("CustomerID", loyalty_sql)

    def test_unlinked_stay_falls_back_to_the_room_pointer(self):
        _result, conn = self._lookup(customer_id=None)
        loyalty_sql = next(q for q in conn.queries if "FROM LOYALTYACCOUNTS" in q.upper())
        self.assertIn("RoomNumber", loyalty_sql)


if __name__ == "__main__":
    unittest.main()
