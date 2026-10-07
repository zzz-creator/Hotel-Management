import os
import unittest

import clearance


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


if __name__ == "__main__":
    unittest.main()
