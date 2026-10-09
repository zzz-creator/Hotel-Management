# type: ignore
"""Unit tests for the key-card panel shared by check-in and "My Key Card".

Check-in and the guest's "My Key Card" view must render the SAME card, and that
panel must state the card number -- the number the guest has to carry and read
out. `_render_key_card` is the single renderer, and `show_active_key_card` is the
lookup wrapper both callers use.

Run with:  python -m unittest discover -s tests
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import keycards


def _card(number="KC-482913", room="10203", status="Active", expires=datetime(2026, 10, 12)):
    return SimpleNamespace(CardNumber=number, RoomNumber=room, Status=status, ExpiresAt=expires)


class RenderKeyCardTests(unittest.TestCase):
    def _panel(self, card, title="My Key Card"):
        with mock.patch.object(keycards.ui, "box") as box:
            keycards._render_key_card(card, title)
        box.assert_called_once()
        return box.call_args[0]

    def test_panel_states_the_card_number_and_stay_details(self):
        title, content = self._panel(_card())
        self.assertEqual(title, "My Key Card")
        self.assertIn("KC-482913", content)
        self.assertIn("10203", content)
        self.assertIn("Active", content)
        self.assertIn("2026-10-12", content)

    def test_missing_expiry_reads_as_end_of_stay(self):
        _title, content = self._panel(_card(expires=None))
        self.assertIn("end of stay", content)

    def test_title_is_passed_through(self):
        # Check-in titles the panel "Your Key Card"; the guest view keeps the default.
        title, _content = self._panel(_card(), title="Your Key Card")
        self.assertEqual(title, "Your Key Card")


class ShowActiveKeyCardTests(unittest.TestCase):
    def test_renders_the_active_card_and_returns_it(self):
        card = _card()
        with mock.patch.object(keycards, "get_active_key_card", return_value=card) as fetch, \
                mock.patch.object(keycards.ui, "box") as box:
            shown = keycards.show_active_key_card("10203", title="Your Key Card")
        fetch.assert_called_once_with("10203")
        box.assert_called_once()
        self.assertIs(shown, card)

    def test_no_active_card_renders_nothing_and_returns_none(self):
        with mock.patch.object(keycards, "get_active_key_card", return_value=None), \
                mock.patch.object(keycards.ui, "box") as box:
            self.assertIsNone(keycards.show_active_key_card("10203"))
        box.assert_not_called()


if __name__ == "__main__":
    unittest.main()
