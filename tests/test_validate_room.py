# type: ignore
"""Unit tests for validate_room() -- the guest identity check in maincopycopy.py.

Identity is last name + first name + room number, matched in one query, and the guest is
never shown a list of other stays. The database and the prompts are both stubbed so the
module imports and runs without SQL Server.

Run with:  python -m unittest discover -s tests
"""

import builtins
import io
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maincopycopy as app


class FakeRow:
    """Stands in for a pyodbc Row; validate_room() only needs attribute access."""

    def __init__(self, room_number, first_name):
        self.RoomNumber = room_number
        self.FirstName = first_name


class FakeDB:
    """A get_connection() stand-in that honours the LastName/FirstName WHERE clause.

    Filtering in the fake rather than returning a fixed list is what makes these tests
    meaningful: a wrong first name really does come back with no rows, exactly as SQL
    would. Comparison is case-insensitive to mirror the database collation.
    """

    def __init__(self, stays=(), fail=False):
        self.stays = [tuple(s) for s in stays]  # (room, last, first)
        self.fail = fail
        self.queries = []

    def connection(self):
        db = self

        class FakeCursor:
            def execute(self, sql, params=()):
                db.queries.append((sql, params))
                if db.fail:
                    raise RuntimeError("simulated database outage")
                last, first = params[0], params[1]
                self._rows = [
                    FakeRow(room, stored_first)
                    for room, stored_last, stored_first in db.stays
                    if str(stored_last).lower() == last.lower()
                    and str(stored_first).lower() == first.lower()
                ]

            def fetchall(self):
                return getattr(self, "_rows", [])

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return FakeCursor()

        return FakeConn()


class TooManyPrompts(BaseException):
    """Raised when validate_room() prompts far more than any test expects.

    Deliberately a BaseException: validate_room() catches Exception around each attempt, so
    an AssertionError here would be swallowed and the test would pass for the wrong reason.
    """


def run_validate(answers, db=None, prompt_limit=30):
    """Drive validate_room() with scripted prompts; return (result, prompts).

    Once the scripted answers run out the stub returns blanks, so a loop that should have
    stopped keeps going and trips TooManyPrompts instead of silently 'succeeding'.
    `prompt_limit` is an absolute number, never derived from the constant under test.
    """
    db = db or FakeDB([("9012", "Smith", "Alice")])
    prompts = []
    it = iter(answers)

    def fake_input(prompt=""):
        prompts.append(prompt)
        if len(prompts) > prompt_limit:
            raise TooManyPrompts(f"too many prompts (last was {prompt!r})")
        return next(it, "")

    with mock.patch.object(app, "get_connection", side_effect=db.connection), \
         mock.patch.object(builtins, "input", fake_input), \
         mock.patch.object(app.time, "sleep", lambda _s: None):
        result = app.validate_room()
    return result, prompts


def logged_output(fn):
    """Run fn() with logging captured, returning everything written."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


class ValidateRoomTests(unittest.TestCase):
    def test_exact_match_returns_room_and_stored_first_name(self):
        result, _ = run_validate(["Smith", "Alice", "9012"])
        self.assertEqual(result, ("9012", "Alice"))

    def test_prompts_for_all_three_fields_in_order(self):
        _, prompts = run_validate(["Smith", "Alice", "9012"])
        self.assertEqual(len(prompts), 3)
        self.assertIn("last name", prompts[0].lower())
        self.assertIn("first name", prompts[1].lower())
        self.assertIn("room number", prompts[2].lower())

    def test_lookup_uses_both_names_in_the_query(self):
        db = FakeDB([("9012", "Smith", "Alice")])
        run_validate(["Smith", "Alice", "9012"], db)
        self.assertEqual(len(db.queries), 1)
        _sql, params = db.queries[0]
        self.assertEqual(params, ("Smith", "Alice"))

    def test_stored_casing_is_returned_not_the_typed_version(self):
        # The collation matches case-insensitively; the guest should see the real spelling.
        result, _ = run_validate(["SMITH", "alice", "9012"])
        self.assertEqual(result, ("9012", "Alice"))

    def test_wrong_first_name_is_refused(self):
        result, _ = run_validate(["Smith", "NotAlice", "9012"])
        self.assertEqual(result, (None, None))

    def test_wrong_last_name_is_refused(self):
        result, _ = run_validate(["Jones", "Alice", "9012"])
        self.assertEqual(result, (None, None))

    def test_room_number_format_variance_is_accepted(self):
        # '01001' and '1001' are the same room, so leading zeros are not a user error.
        db = FakeDB([("1001", "Smith", "Alice")])
        result, _ = run_validate(["Smith", "Alice", "01001"], db)
        self.assertEqual(result, ("01001", "Alice"))

    def test_duplicate_name_in_two_rooms_picks_the_typed_room(self):
        # One person holding two stays is disambiguated by room number, which is why no
        # list of other stays is ever shown.
        db = FakeDB([("9012", "Smith", "Alice"), ("9015", "Smith", "Alice")])
        result, _ = run_validate(["Smith", "Alice", "9015"], db)
        self.assertEqual(result, ("9015", "Alice"))

    def test_room_not_held_by_that_person_is_refused(self):
        db = FakeDB([("9012", "Smith", "Alice"), ("9015", "Smith", "Alice")])
        result, _ = run_validate(["Smith", "Alice", "7777"], db)
        self.assertEqual(result, (None, None))

    def test_gives_up_after_the_attempt_cap(self):
        # Never an infinite loop: the caller gets None and can return to the menu.
        # The counts are spelled out on purpose -- deriving them from
        # VALIDATE_ROOM_MAX_ATTEMPTS would make the assertion true for any cap value.
        answers = ["Smith", "Wrong", "9012"] * 3
        result, prompts = run_validate(answers)
        self.assertEqual(result, (None, None))
        self.assertEqual(len(prompts), 9)
        self.assertEqual(app.VALIDATE_ROOM_MAX_ATTEMPTS, 3)

    def test_cap_constant_is_consulted_at_call_time(self):
        # Proves the cap is genuinely read per call rather than hard-coded to 3, so
        # changing VALIDATE_ROOM_MAX_ATTEMPTS really changes the guest experience.
        with mock.patch.object(app, "VALIDATE_ROOM_MAX_ATTEMPTS", 5):
            result, prompts = run_validate(["Smith", "Wrong", "9012"] * 5)
        self.assertEqual(result, (None, None))
        self.assertEqual(len(prompts), 15)

    def test_succeeds_on_a_later_attempt(self):
        result, _ = run_validate(["Jones", "Nobody", "1", "Smith", "Alice", "9012"])
        self.assertEqual(result, ("9012", "Alice"))

    def test_blank_field_is_rejected_without_querying(self):
        db = FakeDB([("9012", "Smith", "Alice")])
        run_validate(["Smith", "   ", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS, db)
        self.assertEqual(db.queries, [], "a blank first name must not hit the database")

    def test_database_outage_returns_none_rather_than_looping(self):
        db = FakeDB([("9012", "Smith", "Alice")], fail=True)
        result, prompts = run_validate(["Smith", "Alice", "9012"] * 3, db)
        self.assertEqual(result, (None, None))
        # One attempt only: a database fault must not be retried.
        self.assertEqual(len(prompts), 3)

    def test_failure_message_does_not_confirm_the_surname_exists(self):
        # The wording must not distinguish "unknown surname" from "wrong first name",
        # otherwise the prompt leaks who is staying in the hotel.
        output = logged_output(
            lambda: run_validate(["Nosuchsurname", "Nobody", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS)
        ).lower()
        self.assertIn("could not find a reservation", output)
        self.assertNotIn("no reservations found with that last name", output)
        self.assertNotIn("no such surname", output)

    def test_never_prints_a_list_of_other_reservations(self):
        # The old flow listed every stay sharing a surname, which disclosed other guests'
        # room numbers and first names to anyone who knew a surname.
        db = FakeDB([("9012", "Smith", "Alice"), ("9015", "Smith", "Bob"),
                     ("9020", "Smith", "Carol")])
        output = logged_output(
            lambda: run_validate(["Smith", "Dave", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS, db)
        ).lower()
        self.assertNotIn("available reservations with the same last name", output)
        self.assertNotIn("select the reservation number", output)
        for other_room, other_first in (("9015", "bob"), ("9020", "carol")):
            self.assertNotIn(other_room, output)
            self.assertNotIn(other_first, output)


if __name__ == "__main__":
    unittest.main()
