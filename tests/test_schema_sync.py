# type: ignore
"""Tests for tests/check_schema_sync.py itself.

The schema checker is a regex-based guard over the project's SQL, and it silently passes
when its own parser stops matching -- a checker that reports 0 issues because it parsed
nothing is worse than no checker. These tests feed it deliberately broken SQL and assert it
notices, so a parser regression shows up as a test failure rather than as false confidence.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))

import check_schema_sync as checker  # noqa: E402

INDEX_SQL = """
CREATE UNIQUE INDEX UX_Thing_Ref ON dbo.Thing (Ref)
    WHERE Kind IN ('Deposit', 'Prepayment');
GO
CREATE UNIQUE INDEX UX_Thing_Email ON [dbo].[Thing] ([Email] ASC) WHERE Email IS NOT NULL;
GO
CREATE INDEX IX_Thing_Other ON dbo.Thing (Other);
"""


class ParseUniqueIndexTests(unittest.TestCase):
    def test_reads_a_migration_style_index(self):
        found = checker.parse_unique_indexes(INDEX_SQL, False)
        self.assertEqual(found['UX_Thing_Ref'],
                         ('Thing', ('Ref',), True, ('Deposit', 'Prepayment')))

    def test_reads_a_database_sql_style_index_with_sort_direction(self):
        # database.sql is SSMS-generated: bracketed table, and an explicit ASC that must not
        # be mistaken for part of the column name.
        found = checker.parse_unique_indexes(INDEX_SQL, True)
        self.assertEqual(found['UX_Thing_Email'], ('Thing', ('Email',), True, ()))

    def test_a_filter_with_no_literals_is_still_recognised_as_filtered(self):
        # `Email IS NOT NULL` has no quoted literals. Comparing only literals would let a
        # silently unfiltered index pass, which breaks the second name-only walk-in profile.
        found = checker.parse_unique_indexes(INDEX_SQL, True)
        self.assertTrue(found['UX_Thing_Email'][2])

    def test_non_unique_indexes_are_ignored(self):
        found = checker.parse_unique_indexes(INDEX_SQL, True)
        self.assertNotIn('IX_Thing_Other', found)

    def test_a_terminator_ended_predicate_does_not_swallow_the_next_statement(self):
        sql = ("CREATE UNIQUE INDEX UX_A ON dbo.T (A) WHERE K IN ('x'); END\nGO\n"
               "CREATE UNIQUE INDEX UX_B ON dbo.T (B);")
        found = checker.parse_unique_indexes(sql, False)
        self.assertEqual(found['UX_A'][3], ('x',))
        self.assertEqual(found['UX_B'][3], ())
        self.assertFalse(found['UX_B'][2])


class FilterLiteralTests(unittest.TestCase):
    def test_literals_are_order_insensitive(self):
        self.assertEqual(checker._filter_literals("K IN ('b', 'a')"),
                         checker._filter_literals("K IN ('a', 'b')"))

    def test_multicolumn_filter_collects_every_literal(self):
        self.assertEqual(checker._filter_literals("A = 'x' OR B = 'y'"), ('x', 'y'))

    def test_no_literals_yields_empty(self):
        self.assertEqual(checker._filter_literals("Email IS NOT NULL"), ())


class IndexColumnTests(unittest.TestCase):
    def test_brackets_and_direction_are_stripped(self):
        self.assertEqual(checker._index_columns('[Email] ASC, [Name] DESC'), ('Email', 'Name'))

    def test_bare_columns_pass_through(self):
        self.assertEqual(checker._index_columns('Email, Name'), ('Email', 'Name'))

    def test_empty_entries_are_dropped(self):
        self.assertEqual(checker._index_columns('Email, , Name'), ('Email', 'Name'))


class ProjectSqlIsParsedTests(unittest.TestCase):
    """The parsers must find the real thing in this repository, not just in fixtures.

    Without these, a regex that stopped matching the project's own SQL would leave every
    comparison above green while `check_schema_sync.py` reported nothing to check.
    """

    def setUp(self):
        # The checker resolves 'migrations/*.sql' and 'sql/database.sql' relative to the CWD,
        # so run from the repository root regardless of where the suite was invoked.
        self._cwd = os.getcwd()
        os.chdir(ROOT)
        self.addCleanup(os.chdir, self._cwd)

    def test_migration_019_and_020_columns_are_parsed(self):
        self.assertIn('CustomerID', checker.parse_migration_alters('019')[0]['LoyaltyAccounts'])
        self.assertIn('AppliedAmount', checker.parse_migration_alters('020')[0]['ReservationPayments'])

    def test_both_filtered_unique_indexes_are_found(self):
        found = {}
        for path in sorted((ROOT / 'migrations').glob('*.sql')):
            found.update(checker.parse_unique_indexes(path.read_text(encoding='utf-8'), False))
        self.assertIn('UX_CustomerProfiles_Email', found)
        self.assertIn('UX_ReservationPayments_BookingRef_Charge', found)
        self.assertEqual(found['UX_ReservationPayments_BookingRef_Charge'][3],
                         ('Deposit', 'Prepayment'))

    def test_database_sql_parses_the_same_two_indexes(self):
        found = checker.parse_unique_indexes(
            (ROOT / 'sql' / 'database.sql').read_text(encoding='utf-8'), True)
        self.assertIn('UX_CustomerProfiles_Email', found)
        self.assertIn('UX_ReservationPayments_BookingRef_Charge', found)

    def test_the_email_index_is_filtered_in_both_files(self):
        migration = checker.parse_unique_indexes(
            (ROOT / 'migrations' / '019_customer_identity.sql').read_text(encoding='utf-8'), False)
        database = checker.parse_unique_indexes(
            (ROOT / 'sql' / 'database.sql').read_text(encoding='utf-8'), True)
        for found in (migration, database):
            self.assertTrue(found['UX_CustomerProfiles_Email'][2],
                            'the Email unique index must stay FILTERED in both files')


if __name__ == '__main__':
    unittest.main()
