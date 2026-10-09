# type: ignore
"""Unit tests for the key-card panel and for issue_key_card()'s issue rules.

Two behaviours are pinned here:

  * The panel shared by check-in and "My Key Card" renders the same card, and it
    states the card number -- the number the guest has to carry and read out.
    `_render_key_card` is the single renderer; `show_active_key_card` is the
    lookup wrapper both callers use.

  * `issue_key_card()` refuses a room that is not checked in today (no active
    reservation window, or a tracked room that housekeeping has not marked
    Occupied), and retries a fresh number if the INSERT collides with
    UQ_KeyCards_CardNumber.

Run with:  python -m unittest discover -s tests
"""
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyodbc
import keycards
from patch_main import patch_main


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


class _FakeCursor:
    """Answers the few queries issue_key_card() issues, in statement order."""

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._rows = []
        self._index = 0

    def execute(self, sql, params=()):
        normalised = " ".join(sql.split())
        self.conn.log.append((normalised, params))
        upper = normalised.upper()
        self.rowcount = 0
        self._rows = []
        self._index = 0
        if "FROM RESERVATIONS" in upper:
            self._rows = list(self.conn.stay_rows)
        elif "FROM ROOMS" in upper:
            self._rows = list(self.conn.room_rows)
        elif upper.startswith("INSERT") and "KEYCARDS" in upper:
            self.conn.insert_calls += 1
            if self.conn.insert_failures:
                failure = self.conn.insert_failures.pop(0)
                if failure:
                    raise failure
            self.rowcount = 1
        return self

    def fetchone(self):
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """A connection whose cursor reads stay/room rows off this object."""

    def __init__(self, stay_rows=None, room_rows=None, insert_failures=None):
        self.log = []
        self.stay_rows = list(stay_rows or [])
        self.room_rows = list(room_rows or [])
        self.insert_failures = list(insert_failures or [])
        self.insert_calls = 0
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


class IssueKeyCardTests(unittest.TestCase):
    """A card is only issued for a stay that is checked in today."""

    TODAY = date(2026, 5, 1)

    def _issue(self, conn, numbers=("KC-000001",)):
        with patch_main("get_connection", return_value=conn), \
                mock.patch.object(keycards.core, "business_date", return_value=self.TODAY), \
                mock.patch.object(keycards.core, "log_audit"), \
                mock.patch.object(keycards, "_new_card_number", side_effect=list(numbers)):
            return keycards.issue_key_card("10203", "Doe", "Ada")

    def _insert_params(self, conn):
        return next(p for sql, p in conn.log
                    if sql.upper().startswith("INSERT") and "KEYCARDS" in sql.upper())

    def test_issues_when_the_guest_is_checked_in(self):
        conn = _FakeConn(stay_rows=[(date(2026, 5, 1), date(2026, 5, 4))],
                         room_rows=[("Occupied",)])
        self.assertEqual(self._issue(conn), "KC-000001")
        self.assertTrue(conn.committed)
        # Expiry is the check-out day plus one, so the card works through the last night.
        self.assertEqual(self._insert_params(conn)[5], datetime(2026, 5, 5))

    def test_refuses_when_no_stay_exists(self):
        conn = _FakeConn(stay_rows=[])
        self.assertIsNone(self._issue(conn))
        self.assertFalse(conn.committed)
        self.assertEqual(conn.insert_calls, 0)

    def test_refuses_when_the_stay_is_not_current(self):
        # A completed stay: the card must not be reissued against it.
        conn = _FakeConn(stay_rows=[(date(2026, 1, 1), date(2026, 1, 3))],
                         room_rows=[("Occupied",)])
        self.assertIsNone(self._issue(conn))
        self.assertFalse(conn.committed)
        self.assertEqual(conn.insert_calls, 0)

    def test_refuses_when_the_room_is_not_occupied(self):
        conn = _FakeConn(stay_rows=[(date(2026, 5, 1), date(2026, 5, 4))],
                         room_rows=[("Available",)])
        self.assertIsNone(self._issue(conn))
        self.assertFalse(conn.committed)
        self.assertEqual(conn.insert_calls, 0)

    def test_retries_a_fresh_number_on_a_uniqueness_collision(self):
        conn = _FakeConn(
            stay_rows=[(date(2026, 5, 1), date(2026, 5, 4))],
            room_rows=[("Occupied",)],
            insert_failures=[pyodbc.IntegrityError("23000", "duplicate key")],
        )
        self.assertEqual(self._issue(conn, numbers=("KC-000001", "KC-000002")), "KC-000002")
        self.assertEqual(conn.insert_calls, 2)
        self.assertTrue(conn.committed)


if __name__ == "__main__":
    unittest.main()
