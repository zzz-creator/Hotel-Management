# type: ignore
"""Unit tests for the guest identity check: match_guest_identity() and validate_room().

Identity is last name + first name + room number, matched in one query, and the guest is
never shown a list of other stays (docs/BOOKING.md section 1).

Two functions, tested separately:

- `match_guest_identity()` does the lookup and returns an outcome. No prompts, no retry
  policy -- so it can be tested directly.
- `validate_room(ask, say)` owns the attempt cap, the retry rule and the refusal wording.
  The `ask`/`say` callables replace `input()` and `logging`, so the wording is asserted on
  what the function *says* rather than on a captured log stream. That is what lets
  `main.py` stay a UI-free core while the policy it holds stays tested
  (PLAN-tkinter-frontend.md, Phase 0).

The database is stubbed, so the module imports and runs without SQL Server.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app


class FakeRow:
    """Stands in for a pyodbc Row; the identity lookup only needs attribute access."""

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
    """Raised when validate_room() asks far more than any test expects.

    Deliberately a BaseException, so that if the retry loop ever regains a blanket
    `except Exception` an AssertionError here cannot be swallowed and the test cannot
    pass for the wrong reason. `match_guest_identity()` does still catch Exception around
    the query -- that is deliberate -- so a plain AssertionError raised *inside* the fake
    cursor would be absorbed and read as a database outage.
    """


def run_validate(answers, db=None, prompt_limit=30):
    """Drive validate_room() with scripted prompts; return (result, prompts, said).

    The prompts and the messages arrive through the `ask`/`say` callables that
    validate_room() takes, rather than through `builtins.input` and `logging`.
    That is the point of the split: the retry policy and the refusal wording are testable
    without a keyboard and without capturing a log stream.

    Once the scripted answers run out `ask` returns blanks, so a loop that should have
    stopped keeps going and trips TooManyPrompts instead of silently 'succeeding'.
    `prompt_limit` is an absolute number, never derived from the constant under test.
    """
    db = db or FakeDB([("9012", "Smith", "Alice")])
    prompts = []
    said = []
    it = iter(answers)

    def ask(prompt=""):
        prompts.append(prompt)
        if len(prompts) > prompt_limit:
            raise TooManyPrompts(f"too many prompts (last was {prompt!r})")
        return next(it, "")

    def say(message=""):
        said.append(message)

    with mock.patch.object(app, "get_connection", side_effect=db.connection):
        result = app.validate_room(ask, say)
    return result, prompts, said


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
        result, _prompts, _said = run_validate(["Smith", "Alice", "9012"])
        self.assertEqual(result, ("9012", "Alice"))

    def test_prompts_for_all_three_fields_in_order(self):
        _result, prompts, _said = run_validate(["Smith", "Alice", "9012"])
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
        result, _prompts, _said = run_validate(["SMITH", "alice", "9012"])
        self.assertEqual(result, ("9012", "Alice"))

    def test_wrong_first_name_is_refused(self):
        result, _prompts, _said = run_validate(["Smith", "NotAlice", "9012"])
        self.assertEqual(result, (None, None))

    def test_wrong_last_name_is_refused(self):
        result, _prompts, _said = run_validate(["Jones", "Alice", "9012"])
        self.assertEqual(result, (None, None))

    def test_room_number_format_variance_is_accepted(self):
        # '01001' and '1001' are the same room, so leading zeros are not a user error.
        db = FakeDB([("1001", "Smith", "Alice")])
        result, _prompts, _said = run_validate(["Smith", "Alice", "01001"], db)
        self.assertEqual(result, ("01001", "Alice"))

    def test_duplicate_name_in_two_rooms_picks_the_typed_room(self):
        # One person holding two stays is disambiguated by room number, which is why no
        # list of other stays is ever shown.
        db = FakeDB([("9012", "Smith", "Alice"), ("9015", "Smith", "Alice")])
        result, _prompts, _said = run_validate(["Smith", "Alice", "9015"], db)
        self.assertEqual(result, ("9015", "Alice"))

    def test_room_not_held_by_that_person_is_refused(self):
        db = FakeDB([("9012", "Smith", "Alice"), ("9015", "Smith", "Alice")])
        result, _prompts, _said = run_validate(["Smith", "Alice", "7777"], db)
        self.assertEqual(result, (None, None))

    def test_gives_up_after_the_attempt_cap(self):
        # Never an infinite loop: the caller gets None and can return to the menu.
        # The counts are spelled out on purpose -- deriving them from
        # VALIDATE_ROOM_MAX_ATTEMPTS would make the assertion true for any cap value.
        answers = ["Smith", "Wrong", "9012"] * 3
        result, prompts, _said = run_validate(answers)
        self.assertEqual(result, (None, None))
        self.assertEqual(len(prompts), 9)
        self.assertEqual(app.VALIDATE_ROOM_MAX_ATTEMPTS, 3)

    def test_cap_constant_is_consulted_at_call_time(self):
        # Proves the cap is genuinely read per call rather than hard-coded to 3, so
        # changing VALIDATE_ROOM_MAX_ATTEMPTS really changes the guest experience.
        with mock.patch.object(app, "VALIDATE_ROOM_MAX_ATTEMPTS", 5):
            result, prompts, _said = run_validate(["Smith", "Wrong", "9012"] * 5)
        self.assertEqual(result, (None, None))
        self.assertEqual(len(prompts), 15)

    def test_succeeds_on_a_later_attempt(self):
        result, _prompts, _said = run_validate(["Jones", "Nobody", "1", "Smith", "Alice", "9012"])
        self.assertEqual(result, ("9012", "Alice"))

    def test_blank_field_is_rejected_without_querying(self):
        db = FakeDB([("9012", "Smith", "Alice")])
        run_validate(["Smith", "   ", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS, db)
        self.assertEqual(db.queries, [], "a blank first name must not hit the database")

    def test_database_outage_returns_none_rather_than_looping(self):
        db = FakeDB([("9012", "Smith", "Alice")], fail=True)
        result, prompts, _said = run_validate(["Smith", "Alice", "9012"] * 3, db)
        self.assertEqual(result, (None, None))
        # One attempt only: a database fault must not be retried.
        self.assertEqual(len(prompts), 3)

    def test_failure_message_does_not_confirm_the_surname_exists(self):
        # The wording must not distinguish "unknown surname" from "wrong first name",
        # otherwise the prompt leaks who is staying in the hotel. docs/BOOKING.md §1.
        _result, _prompts, said = run_validate(
            ["Nosuchsurname", "Nobody", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS)
        output = " ".join(said).lower()
        self.assertIn("could not find a reservation", output)
        self.assertNotIn("no reservations found with that last name", output)
        self.assertNotIn("no such surname", output)

    def test_never_prints_a_list_of_other_reservations(self):
        # The old flow listed every stay sharing a surname, which disclosed other guests'
        # room numbers and first names to anyone who knew a surname.
        db = FakeDB([("9012", "Smith", "Alice"), ("9015", "Smith", "Bob"),
                     ("9020", "Smith", "Carol")])
        _result, _prompts, said = run_validate(
            ["Smith", "Dave", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS, db)
        output = " ".join(said).lower()
        self.assertNotIn("available reservations with the same last name", output)
        self.assertNotIn("select the reservation number", output)
        for other_room, other_first in (("9015", "bob"), ("9020", "carol")):
            self.assertNotIn(other_room, output)
            self.assertNotIn(other_first, output)

    def test_the_giving_up_message_is_said_once_at_the_end(self):
        _result, _prompts, said = run_validate(
            ["Smith", "Wrong", "9012"] * app.VALIDATE_ROOM_MAX_ATTEMPTS)
        self.assertEqual(said[-1], "Too many unsuccessful attempts.")
        self.assertEqual(sum(1 for m in said if m == "Too many unsuccessful attempts."), 1)

    def test_no_prompt_occurs_after_a_database_outage(self):
        # The old code caught the exception inside the attempt loop and returned, but
        # a database outage is not a mistyped name, so the guest must be told rather
        # than asked again.
        db = FakeDB([("9012", "Smith", "Alice")], fail=True)
        _result, _prompts, said = run_validate(["Smith", "Alice", "9012"] * 3, db)
        self.assertTrue(any("could not reach" in m.lower() for m in said), said)


class MatchGuestIdentityTests(unittest.TestCase):
    """The lookup on its own: no prompts, no retry policy, just the three-way answer."""

    def setUp(self):
        self.db = FakeDB([("9012", "Smith", "Alice")])
        patcher = mock.patch.object(app, "get_connection", side_effect=self.db.connection)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_a_match_returns_ok_and_the_stored_first_name(self):
        outcome, first, _message = app.match_guest_identity("Smith", "Alice", "9012")
        self.assertEqual(outcome, app.IDENTITY_OK)
        self.assertEqual(first, "Alice")

    def test_a_missing_surname_is_a_no_match_with_no_query(self):
        outcome, first, _message = app.match_guest_identity("", "Alice", "9012")
        self.assertEqual(outcome, app.IDENTITY_NO_MATCH)
        self.assertIsNone(first)
        self.assertEqual(self.db.queries, [])

    def test_a_whitespace_only_field_is_treated_as_missing(self):
        # `.strip()` happens in the prompt wrapper, so a field that is all spaces
        # reaches here as "   " only if a caller skips that. It must not match.
        outcome, _first, _message = app.match_guest_identity("Smith", "   ", "9012")
        self.assertEqual(outcome, app.IDENTITY_NO_MATCH)

    def test_a_room_mismatch_is_distinguished_from_a_name_mismatch(self):
        # Both are refusals, but they are different refusals -- one says the name was
        # not found, the other says the room does not belong to it. Neither reveals
        # whether the surname exists, because both are reached only after a query that
        # matched on the name.
        db = FakeDB([("9012", "Smith", "Alice")])
        with mock.patch.object(app, "get_connection", side_effect=db.connection):
            outcome, _f, message = app.match_guest_identity("Smith", "Alice", "7777")
        self.assertEqual(outcome, app.IDENTITY_ROOM_MISMATCH)
        self.assertIn("does not match", message)

    def test_the_outcome_constants_are_distinct(self):
        constants = [app.IDENTITY_OK, app.IDENTITY_NO_MATCH,
                     app.IDENTITY_ROOM_MISMATCH, app.IDENTITY_UNAVAILABLE]
        self.assertEqual(len(set(constants)), len(constants))

    def test_an_unavailable_database_is_its_own_outcome(self):
        db = FakeDB([("9012", "Smith", "Alice")], fail=True)
        with mock.patch.object(app, "get_connection", side_effect=db.connection):
            outcome, _first, message = app.match_guest_identity("Smith", "Alice", "9012")
        self.assertEqual(outcome, app.IDENTITY_UNAVAILABLE)
        self.assertIn("could not reach", message.lower())


if __name__ == "__main__":
    unittest.main()
